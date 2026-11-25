"""端到端測試：真的啟動 AUN server，用 plugin 的 hook 腳本和 MCP client 模擬兩個 agent。"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aun.app import create_app
from aun.config import Config
from aun.hub import Hub

HOOK = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "aun_hook.py"
PROJECT = "demo"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = free_port()
    cfg = Config(db_path=str(tmp_path / "aun.db"), host="127.0.0.1", port=port, progress_interval=0,
                 summary_idle=1200, llm_base_url="", llm_api_key="", llm_models=(),
                 discord_token="", discord_post_summaries=False)
    tokens = {}
    setup = Hub(cfg)
    tokens["justin-agent"] = setup.add_agent("justin-agent", "justin", owner_discord="111")
    tokens["partner-agent"] = setup.add_agent("partner-agent", "partner", owner_discord="222")
    setup.db.close()

    app = create_app(cfg)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "tokens": tokens, "hub": app.state.hub, "tmp": tmp_path,
           "summarizer": app.state.summarizer}
    srv.should_exit = True
    thread.join(timeout=5)


def run_hook(srv, agent: str, mode: str, payload: dict, repo: Path) -> dict | None:
    env = {**os.environ, "AUN_URL": srv["url"], "AUN_TOKEN": srv["tokens"][agent], "AUN_PROJECT": PROJECT,
           "AUN_STATE_DIR": str(srv["tmp"] / f"state-{agent}"), "CLAUDE_PROJECT_DIR": str(repo)}
    out = subprocess.run([sys.executable, str(HOOK), mode], input=json.dumps(payload), env=env,
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def context_of(output: dict | None) -> str:
    return output["hookSpecificOutput"]["additionalContext"] if output else ""


def wait_for(cond, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("等待逾時")


def make_repo(path: Path) -> Path:
    path.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def transcript_line(kind: str, content) -> str:
    entry = {"type": kind, "message": {"role": kind, "content": content}}
    if kind == "user":
        entry["origin"] = {"kind": "human"}
    return json.dumps(entry, ensure_ascii=False) + "\n"


def test_two_agents_full_flow(server):
    srv, hub = server, server["hub"]
    repo_j = make_repo(srv["tmp"] / "repo-justin")
    repo_p = make_repo(srv["tmp"] / "repo-partner")

    # 情境 1：兩邊各自開 session，拿到專案全貌
    ov = context_of(run_hook(srv, "justin-agent", "session-start", {"session_id": "sj", "source": "startup"}, repo_j))
    assert '<aun project="demo" type="overview">' in ov and "justin-agent" in ov
    run_hook(srv, "partner-agent", "session-start", {"session_id": "sp", "source": "startup"}, repo_p)

    # 情境 2：沒有新消息 → 不輸出任何東西
    assert run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "hi"}, repo_j) is None

    # 搭檔那邊做了一輪工作：改檔案 + commit
    tp = srv["tmp"] / "partner.jsonl"
    tp.write_text(
        transcript_line("user", "把 auth 改成 JWT")
        + transcript_line("assistant", [
            {"type": "text", "text": "好，我來改 auth。"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo_p / "src/auth.ts")}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}])
        + transcript_line("user", "<local-command-stdout>ignored</local-command-stdout>")
    )
    stop_payload = {"session_id": "sp", "transcript_path": str(tp)}
    run_hook(srv, "partner-agent", "stop", stop_payload, repo_p)  # 第一次：記下 HEAD
    wait_for(lambda: hub.db.execute("SELECT COUNT(*) FROM items WHERE kind='turn'").fetchone()[0] == 1)
    turn = hub.db.execute("SELECT * FROM items WHERE kind='turn'").fetchone()
    assert "把 auth 改成 JWT" in turn["body"] and "Bash npm test" in turn["body"] and "ignored" not in turn["body"]

    (repo_p / "src").mkdir()
    (repo_p / "src/auth.ts").write_text("jwt\n")
    subprocess.run(["git", "-C", str(repo_p), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_p), "commit", "-qm", "auth 改用 JWT"], check=True)
    with tp.open("a") as f:
        f.write(transcript_line("assistant", [{"type": "text", "text": "已 commit。"}]))
    run_hook(srv, "partner-agent", "stop", stop_payload, repo_p)
    wait_for(lambda: hub.db.execute("SELECT COUNT(*) FROM items WHERE kind='commit'").fetchone()[0] == 1)
    # commit 會在幾秒後觸發摘要（沒有 LLM key → 條列摘要）
    wait_for(lambda: hub.db.execute("SELECT COUNT(*) FROM items WHERE kind='summary'").fetchone()[0] == 1)

    # 情境 3：justin 下一次送訊息時收到搭檔的 commit 和進度
    ctx = context_of(run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "繼續"}, repo_j))
    assert "[partner-agent commit]" in ctx and "auth 改用 JWT" in ctx and "[partner-agent 進度]" in ctx
    # 同樣的東西不會再附第二次
    assert run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "再一次"}, repo_j) is None

    # 情境 7：justin 要改搭檔剛改過的檔案 → 警告；同一個檔案不重複警告
    pre = {"session_id": "sj", "tool_name": "Edit", "tool_input": {"file_path": str(repo_j / "src/auth.ts")}}
    warn = context_of(run_hook(srv, "justin-agent", "pre-tool", pre, repo_j))
    assert "partner-agent" in warn and "src/auth.ts" in warn
    assert run_hook(srv, "justin-agent", "pre-tool", pre, repo_j) is None
    other = {**pre, "tool_input": {"file_path": str(repo_j / "README.md")}}
    assert run_hook(srv, "justin-agent", "pre-tool", other, repo_j) is None

    # 情境 4：人類在 Discord 下結論 → 兩邊都收到，送達狀態更新
    [cid] = hub.human_post(PROJECT, "justin", "111", "錯誤一律回 {code, message}", [], {"thread": "錯誤格式"})
    assert hub.delivery_status([cid]) == [("justin-agent", False), ("partner-agent", False)]
    ctx = context_of(run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "x"}, repo_j))
    assert f"[人類結論 #{cid}] justin（DC #錯誤格式）" in ctx
    assert hub.delivery_status([cid]) == [("justin-agent", True), ("partner-agent", False)]

    # 情境 5：搭檔 tag justin-agent 下指令 → 需要 justin 確認；justin 自己下的則不用
    [rid] = hub.human_post(PROJECT, "partner", "222", "登入 API 改成 /v2/auth/login", ["justin-agent"], {})
    [oid] = hub.human_post(PROJECT, "justin", "111", "順便更新 README", ["justin-agent"], {})
    ctx = context_of(run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "x"}, repo_j))
    assert f"[需你確認 #{rid}]" in ctx and f"[主人指示 #{oid}]" in ctx
    assert f"#{rid}]" not in context_of(
        run_hook(srv, "partner-agent", "prompt", {"session_id": "sp", "prompt": "x"}, repo_p))

    # 情境 6：MCP 工具——justin-agent 問問題、partner-agent 回答、回報指示處理結果
    async def mcp_call(agent: str, tool: str, args: dict) -> str:
        headers = {"Authorization": f"Bearer {srv['tokens'][agent]}", "X-AUN-Project": PROJECT}
        async with httpx2.AsyncClient(headers=headers) as http:
            async with streamable_http_client(f"{srv['url']}/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args)
                    return result.content[0].text

    async def mcp_flow():
        out = await mcp_call("justin-agent", "send_message",
                             {"to": "partner-agent", "body": "refresh token 放哪？", "question": True})
        qid = int(out.split("#")[1].split("。")[0])
        ctx = context_of(run_hook(srv, "partner-agent", "prompt", {"session_id": "sp", "prompt": "x"}, repo_p))
        assert f"[justin-agent 的問題 #{qid}]" in ctx
        await mcp_call("partner-agent", "reply", {"item_id": qid, "body": "httpOnly cookie"})
        ctx = context_of(run_hook(srv, "justin-agent", "prompt", {"session_id": "sj", "prompt": "x"}, repo_j))
        assert f"[partner-agent 回覆 #{qid}]：httpOnly cookie" in ctx
        out = await mcp_call("justin-agent", "resolve_request", {"item_id": rid, "status": "done", "note": "已改"})
        assert "已回報" in out
        assert hub.item(rid)["meta"]["status"] == "done"
        # 不能處理別人的指示
        out = await mcp_call("partner-agent", "resolve_request", {"item_id": rid, "status": "done", "note": "x"})
        assert "不是給你的指示" in out
        out = await mcp_call("partner-agent", "search", {"query": "JWT"})
        assert "auth 改用 JWT" in out

    anyio.run(mcp_flow)

    # 情境 8：壓縮後重新拿到專案全貌，裡面包含已確認的結論
    ov = context_of(run_hook(srv, "justin-agent", "session-start", {"session_id": "sj", "source": "compact"}, repo_j))
    assert "錯誤一律回" in ov and "partner-agent" in ov

    # 同一個 commit 從另一邊（pull 了對方的 fork）再出現，不會重複記錄
    sha = hub.db.execute("SELECT json_extract(meta,'$.sha') s FROM items WHERE kind='commit'").fetchone()["s"]
    assert hub.ingest("justin-agent", PROJECT, [], [{"sha": sha, "subject": "auth 改用 JWT"}]) == 0


def test_bad_token_and_missing_env(server):
    resp = httpx2.post(f"{server['url']}/v1/prompt", json={"project": PROJECT},
                       headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    # 沒有設定 AUN 環境變數的專案：hook 什麼都不做
    out = subprocess.run([sys.executable, str(HOOK), "prompt"], input="{}", capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if not k.startswith("AUN_")})
    assert out.returncode == 0 and out.stdout == ""


def test_hub_offline_spools_and_replays(server, tmp_path):
    srv = server
    repo = make_repo(tmp_path / "r")
    tp = tmp_path / "t.jsonl"
    tp.write_text(transcript_line("user", "離線時做的事") + transcript_line("assistant", [{"type": "text", "text": "ok"}]))
    good_url = srv["url"]
    srv["url"] = "http://127.0.0.1:1"  # 連不上
    run_hook(srv, "justin-agent", "stop", {"session_id": "s", "transcript_path": str(tp)}, repo)
    spool = tmp_path / "state-justin-agent" / "spool"
    wait_for(lambda: spool.exists() and any(spool.glob("*.json")))
    srv["url"] = good_url
    with tp.open("a") as f:
        f.write(transcript_line("user", "恢復連線後"))
    run_hook(srv, "justin-agent", "stop", {"session_id": "s", "transcript_path": str(tp)}, repo)
    hub = srv["hub"]
    wait_for(lambda: hub.db.execute("SELECT COUNT(*) FROM items WHERE kind='turn'").fetchone()[0] == 2)
    bodies = [r["body"] for r in hub.db.execute("SELECT body FROM items WHERE kind='turn' ORDER BY id")]
    assert "離線時做的事" in bodies[0] and "恢復連線後" in bodies[1]
    assert not any(spool.glob("*.json"))

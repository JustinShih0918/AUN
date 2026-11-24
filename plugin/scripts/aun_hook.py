#!/usr/bin/env python3
"""AUN hook：把 Claude Code 的事件同步到 AUN，並把新資訊帶回 context。

用法：aun_hook.py <session-start|prompt|stop|pre-compact|pre-tool>，hook 輸入從 stdin 讀取。
只用標準庫；需要的環境變數：AUN_URL、AUN_TOKEN、AUN_PROJECT（缺任何一個就什麼都不做）。

本地狀態放在 ~/.aun（可用 AUN_STATE_DIR 覆寫）：
  sessions/<session_id>.json  transcript 讀到的位置、是否已附專案全貌、已警告過的檔案
  projects/<project>.json     上次看到的 HEAD、其他 agent 最近改過的檔案（編輯前比對用）
  spool/                      AUN 連不上時暫存的寫入，下次成功連線時補送
"""

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("AUN_URL", "").rstrip("/")
TOKEN = os.environ.get("AUN_TOKEN", "")
PROJECT = os.environ.get("AUN_PROJECT", "")
STATE = Path(os.environ.get("AUN_STATE_DIR", "~/.aun")).expanduser()
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TIMEOUT = 4
WRITE_TIMEOUT = 15


# ── 小工具 ───────────────────────────────────────────────────────────

def post(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        URL + path,
        data=json.dumps({"project": PROJECT, **body}, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def load(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def session_file(session_id: str) -> Path:
    return STATE / "sessions" / f"{session_id or 'unknown'}.json"


def project_file() -> Path:
    return STATE / "projects" / f"{PROJECT}.json"


def project_dir(hook: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()


def emit(event: str, context: str | None) -> None:
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
                         ensure_ascii=False))


def git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def save_touches(touches: dict | None) -> None:
    if touches is None:
        return
    state = load(project_file(), {})
    state["touches"] = touches
    save(project_file(), state)


def detach() -> None:
    """寫入在背景做，不讓 Claude 等。關掉繼承來的 stdio，Claude Code 才不會等這個行程。"""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


# ── 讀：附進 context ─────────────────────────────────────────────────

def read_context(hook: dict, event: str, overview: bool) -> None:
    sfile = session_file(hook.get("session_id", ""))
    session = load(sfile, {})
    if not session.get("overview_done"):
        overview = True
    try:
        resp = post("/v1/session" if overview else "/v1/prompt", {}, READ_TIMEOUT)
    except (OSError, ValueError):
        return  # AUN 連不上就安靜跳過，不影響工作
    save_touches(resp.get("touches"))
    if overview:
        session["overview_done"] = True
        save(sfile, session)
    emit(event, resp.get("context"))


def pre_tool(hook: dict) -> None:
    tool_input = hook.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if hook.get("tool_name") not in EDIT_TOOLS or not path:
        return
    rel = os.path.relpath(path, project_dir(hook)) if os.path.isabs(path) else path
    touch = load(project_file(), {}).get("touches", {}).get(rel)
    if not touch:
        return
    sfile = session_file(hook.get("session_id", ""))
    session = load(sfile, {})
    warned = session.setdefault("warned", {})
    if time.time() - warned.get(rel, 0) < 6 * 3600:
        return  # 同一個檔案 6 小時內只提醒一次
    warned[rel] = time.time()
    save(sfile, session)
    hours = (time.time() - touch["at"]) / 3600
    ago = f"{int(hours * 60)} 分鐘" if hours < 1 else f"{hours:.0f} 小時"
    emit("PreToolUse", f"⚠️ AUN：{touch['agent']} {ago}前也改過 {rel}（{touch['note']}）。"
                       "如果這次的修改和對方的改動範圍重疊，請先告訴使用者。")


# ── 寫：對話增量和 commit ────────────────────────────────────────────

def summarize_tool(block: dict) -> str:
    inp = block.get("input") or {}
    for key in ("file_path", "notebook_path", "command", "pattern", "url", "query", "description", "prompt"):
        if isinstance(inp.get(key), str):
            return f"{block.get('name')} {inp[key][:200]}"
    return str(block.get("name"))


def read_turns(transcript: str, offset: int, root: str) -> tuple[list[dict], int]:
    """從上次讀到的位置往後讀 transcript，整理成一輪一輪：使用者指令、Claude 的文字回覆、工具、改動的檔案。"""
    try:
        with open(transcript, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1  # 只處理完整的行，最後半行留到下次
    turns: list[dict] = []
    current: dict | None = None
    for line in data[:end].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user" and isinstance(content, str):
            origin = entry.get("origin") or {}
            if (origin and origin.get("kind") != "human") or content.lstrip().startswith("<"):
                continue  # 斜線指令輸出、系統注入之類的，不算使用者指令
            current = {"prompt": content, "responses": [], "tools": [], "files": []}
            turns.append(current)
        elif entry.get("type") == "assistant" and isinstance(content, list):
            if current is None:  # 上一批結尾那一輪的延續
                current = {"prompt": None, "responses": [], "tools": [], "files": []}
                turns.append(current)
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    current["responses"].append(block["text"])
                elif block.get("type") == "tool_use":
                    current["tools"].append(summarize_tool(block))
                    inp = block.get("input") or {}
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if block.get("name") in EDIT_TOOLS and isinstance(path, str):
                        rel = os.path.relpath(path, root) if os.path.isabs(path) else path
                        if not rel.startswith("..") and rel not in current["files"]:
                            current["files"].append(rel)
    return turns, offset + end


def new_commits(root: str) -> list[dict]:
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    if not head:
        return []
    state = load(project_file(), {})
    old = state.get("head")
    state["head"] = head
    save(project_file(), state)
    if not old or old == head:
        return []  # 第一次只記下 HEAD，不把整段歷史當成新 commit
    rng = f"{old}..{head}" if git(root, "merge-base", "--is-ancestor", old, head) is not None else head
    out = git(root, "log", "-n", "20", "--format=%x1e%H%x1f%an%x1f%s", "--name-only", rng) or ""
    commits = []
    for record in out.split("\x1e")[1:]:
        header, _, files = record.partition("\n")
        sha, author, subject = (header.split("\x1f") + ["", ""])[:3]
        commits.append({"sha": sha, "author": author, "subject": subject,
                        "files": [f for f in files.splitlines() if f.strip()]})
    return list(reversed(commits))


def flush_spool() -> None:
    spool = STATE / "spool"
    for path in sorted(spool.glob("*.json")) if spool.exists() else []:
        item = load(path, {})
        try:
            post(item["path"], item["body"], WRITE_TIMEOUT)
        except (OSError, ValueError, KeyError):
            return  # 還是連不上，保留順序，下次再試
        path.unlink(missing_ok=True)


def write(hook: dict, endpoint: str) -> None:
    detach()
    time.sleep(1)  # 讓 Claude Code 把這一輪的最後一筆寫進 transcript
    sfile = session_file(hook.get("session_id", ""))
    sfile.parent.mkdir(parents=True, exist_ok=True)
    with open(str(sfile) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # 連續幾輪很快結束時，避免兩個行程讀到同一段
        session = load(sfile, {})
        root = project_dir(hook)
        turns, offset = read_turns(hook.get("transcript_path", ""), session.get("offset", 0), root)
        commits = new_commits(root)
        turns = turns[-50:]  # 剛啟用時（例如 resume 舊 session）只送最近的部分
        session["offset"] = offset
        save(sfile, session)
        if not turns and not commits and endpoint == "/v1/stop":
            return
        flush_spool()
        body = {"turns": turns, "commits": commits}
        try:
            post(endpoint, body, WRITE_TIMEOUT)
        except (OSError, ValueError):
            spool = STATE / "spool"
            spool.mkdir(parents=True, exist_ok=True)
            save(spool / f"{time.time():.6f}.json", {"path": endpoint, "body": body})


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if not (URL and TOKEN and PROJECT):
        return
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return
    if mode == "session-start":
        read_context(hook, "SessionStart", overview=True)
    elif mode == "prompt":
        read_context(hook, "UserPromptSubmit", overview=False)
    elif mode == "pre-tool":
        pre_tool(hook)
    elif mode == "stop":
        write(hook, "/v1/stop")
    elif mode == "pre-compact":
        write(hook, "/v1/compact")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # hook 絕不能讓 Claude 的工作中斷
        pass

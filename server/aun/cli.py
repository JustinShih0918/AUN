"""AUN 管理指令：啟動 server、註冊 agent、綁定 Discord 頻道。

在 docker 裡使用：docker compose exec aun aun agent add justin-agent --owner justin
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
import time

from .config import Config
from .hub import Hub, HubError
from .summarizer import LLMError, Summarizer

LLM_SAMPLE = (
    "test-agent 最新的工作紀錄：\n使用者：把登入改成 JWT\nClaude：已修改 src/auth/jwt.ts，新增 refresh token，"
    "測試通過。\n工具：Edit src/auth/jwt.ts；Bash npm test\n[commit a1b2c3d] auth 改用 JWT"
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="aun", description="AUN：Agent United Nations")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="啟動 AUN server")

    agent = sub.add_parser("agent", help="管理 agent").add_subparsers(dest="action", required=True)
    add = agent.add_parser("add", help="註冊 agent 並產生 token")
    add.add_argument("name", help="agent 名稱，例如 justin-agent")
    add.add_argument("--owner", required=True, help="人類主人的名字，例如 justin")
    add.add_argument("--discord-user", help="主人的 Discord user id 或 username")
    add.add_argument("--discord-role", help="對應的 Discord 身分組 id 或名稱（預設用 agent 名稱比對）")
    agent.add_parser("list", help="列出所有 agent")
    token = agent.add_parser("token", help="重新產生 token（舊的立即失效）")
    token.add_argument("name")
    edit = agent.add_parser("set-discord", help="設定 agent 的 Discord 對應")
    edit.add_argument("name")
    edit.add_argument("--discord-user")
    edit.add_argument("--discord-role")

    sub.add_parser("llm-test", help="用一段範例紀錄測試摘要模型（確認 API key 和模型名稱可用）")

    bind = sub.add_parser("bind", help="把 Discord 頻道綁定到專案（也可以在 Discord 用 @AUN 綁定）")
    bind.add_argument("project")
    bind.add_argument("channel_id")

    args = parser.parse_args()
    cfg = Config.from_env()

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
        return

    if args.cmd == "llm-test":
        asyncio.run(_llm_test(cfg))
        return

    hub = Hub(cfg)
    try:
        if args.cmd == "agent" and args.action == "add":
            token = hub.add_agent(args.name, args.owner, args.discord_user, args.discord_role)
            print(f"已註冊 {args.name}（主人：{args.owner}）\nAUN_TOKEN={token}\n這個 token 只會顯示一次。")
        elif args.cmd == "agent" and args.action == "list":
            for a in hub.agents():
                print(f"{a['name']}\t主人：{a['owner']}\tDiscord 使用者：{a['owner_discord'] or '-'}"
                      f"\t身分組：{a['discord_role'] or '(同名)'}")
        elif args.cmd == "agent" and args.action == "token":
            print(f"AUN_TOKEN={hub.rotate_token(args.name)}")
        elif args.cmd == "agent" and args.action == "set-discord":
            hub.set_agent_discord(args.name, args.discord_user, args.discord_role)
            print("已更新")
        elif args.cmd == "bind":
            hub.bind_channel(args.project, args.channel_id)
            print(f"已將頻道 {args.channel_id} 綁定到 {args.project}")
    except (HubError, sqlite3.IntegrityError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)


async def _llm_test(cfg: Config) -> None:
    if not cfg.llm_api_key:
        sys.exit("錯誤：沒有設定 AUN_LLM_API_KEY")
    print(f"依序嘗試：{' → '.join(cfg.llm_models)}")
    start = time.time()
    try:
        model, text = await Summarizer(None, cfg).complete(LLM_SAMPLE)  # type: ignore[arg-type]
    except LLMError as e:
        sys.exit(f"全部模型都失敗：{e}")
    print(f"✅ {model}（{time.time() - start:.1f} 秒）\n{text}")


if __name__ == "__main__":
    main()

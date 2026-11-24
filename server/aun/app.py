"""組裝 AUN server：REST（hooks 用）+ MCP（agent 用）+ 摘要排程 + Discord bot，全部跑在同一個 event loop。"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware

from .api import TokenAuth, api_routes
from .config import Config
from .hub import Hub
from .mcp_tools import build_mcp
from .summarizer import Summarizer

log = logging.getLogger("aun")


def create_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or Config.from_env()
    hub = Hub(cfg)
    summarizer = Summarizer(hub, cfg)
    hub.listeners.append(summarizer.on_event)

    mcp = build_mcp(hub)
    # 各 container 透過 docker network 用 http://aun:8080 連進來，Host 不是 localhost，所以關掉 DNS rebinding 檢查
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    bot = None
    if cfg.discord_token:
        from .discord_bot import AunBot
        bot = AunBot(hub, cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            summarizer.start()
            bot_task = asyncio.create_task(bot.start(cfg.discord_token)) if bot else None
            log.info("AUN 已啟動（Discord：%s，LLM 摘要：%s）",
                     "開" if bot else "關", cfg.llm_model if cfg.llm_api_key else "關（使用條列摘要）")
            try:
                yield
            finally:
                await summarizer.stop()
                if bot and bot_task:
                    await bot.close()

    app = Starlette(
        routes=api_routes(hub, summarizer) + list(mcp_app.routes),
        middleware=[Middleware(TokenAuth, hub=hub)],
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.summarizer = summarizer
    return app

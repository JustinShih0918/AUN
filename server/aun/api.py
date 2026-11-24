"""給 Claude Code hooks 呼叫的 REST API，以及共用的 token 驗證。"""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .hub import Hub, HubError
from .summarizer import Summarizer


class TokenAuth:
    """/v1 和 /mcp 都要帶 Bearer token；其他路徑（/health）不用。"""

    def __init__(self, app: ASGIApp, hub: Hub):
        self.app = app
        self.hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(("/v1", "/mcp")):
            headers = dict(scope["headers"])
            token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if self.hub.agent_by_token(token) is None:
                await JSONResponse({"error": "AUN token 無效"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def api_routes(hub: Hub, summarizer: Summarizer) -> list[Route]:
    async def context(request: Request) -> tuple[str, str, dict]:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        agent = hub.agent_by_token(token)["name"]
        body = json.loads(await request.body() or b"{}")
        project_id = str(body.get("project", ""))
        hub.ensure_member(agent, project_id)
        return agent, project_id, body

    def endpoint(fn):
        async def wrapped(request: Request) -> JSONResponse:
            try:
                return JSONResponse(await fn(*await context(request)))
            except (HubError, json.JSONDecodeError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return wrapped

    @endpoint
    async def session(agent: str, project_id: str, body: dict) -> dict:
        # SessionStart（含壓縮後）以及 session 裡第一次送訊息
        return {"context": hub.overview(agent, project_id), "touches": hub.touches(agent, project_id)}

    @endpoint
    async def prompt(agent: str, project_id: str, body: dict) -> dict:
        return {"context": hub.inbox(agent, project_id), "touches": hub.touches(agent, project_id)}

    @endpoint
    async def stop(agent: str, project_id: str, body: dict) -> dict:
        new_commits = hub.ingest(agent, project_id, body.get("turns", []), body.get("commits", []))
        return {"ok": True, "new_commits": new_commits}

    @endpoint
    async def compact(agent: str, project_id: str, body: dict) -> dict:
        hub.ingest(agent, project_id, body.get("turns", []), body.get("commits", []))
        summarizer.request(agent, project_id)
        return {"ok": True}

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return [
        Route("/health", health),
        Route("/v1/session", session, methods=["POST"]),
        Route("/v1/prompt", prompt, methods=["POST"]),
        Route("/v1/stop", stop, methods=["POST"]),
        Route("/v1/compact", compact, methods=["POST"]),
    ]

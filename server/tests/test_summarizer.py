"""摘要模型：依序嘗試、失敗時退回條列。用 httpx.MockTransport 模擬 OpenAI 相容 API。"""

import asyncio
import json

import httpx

from aun.config import Config
from aun.hub import Hub
from aun.summarizer import Summarizer


def make(tmp_path, handler, models=("first/model", "backup/model")):
    cfg = Config(db_path=str(tmp_path / "aun.db"), host="", port=0, progress_interval=0, summary_idle=0,
                 llm_base_url="https://llm.test/v1", llm_api_key="k", llm_models=models,
                 discord_token="", discord_post_summaries=False)
    hub = Hub(cfg)
    hub.add_agent("a", "owner")
    hub.ensure_member("a", "p")
    hub.ingest("a", "p", [{"prompt": "把登入改成 JWT", "responses": ["好"], "files": ["src/auth.ts"]}], [])
    return hub, Summarizer(hub, cfg, transport=httpx.MockTransport(handler))


def test_falls_back_to_next_model(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "first/model":
            return httpx.Response(404, json={"detail": "model not found"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>嗯</think>- 改成 JWT"}}]})

    hub, summarizer = make(tmp_path, handler)
    summary_id = asyncio.run(summarizer.summarize("a", "p"))
    assert seen == ["first/model", "backup/model"]
    assert hub.item(summary_id)["body"] == "- 改成 JWT"


def test_all_models_fail_uses_bullet_summary(tmp_path):
    hub, summarizer = make(tmp_path, lambda r: httpx.Response(429, text="rate limited"))
    summary_id = asyncio.run(summarizer.summarize("a", "p"))
    body = hub.item(summary_id)["body"]
    assert "把登入改成 JWT" in body and "src/auth.ts" in body

"""進度摘要：把某個 agent 在某專案的 L0 紀錄和 commit 整理成 L2。

觸發時機：有新 commit（延遲幾秒，讓同一輪的紀錄先進來）、context 壓縮前、閒置超過 summary_idle。
模型走 OpenAI 相容的 /chat/completions；沒設定 API key 或呼叫失敗時，退回不需要 LLM 的條列。
"""

import asyncio
import logging
import re
from typing import Any

import httpx

from .config import Config
from .hub import Hub

log = logging.getLogger("aun.summarizer")

SYSTEM_PROMPT = (
    "你是軟體團隊的進度記錄員。根據一位 AI agent 和它的使用者的工作紀錄，"
    "用繁體中文寫 3～6 個條列，涵蓋：完成了什麼、正在做什麼、遇到的問題或卡住的地方、下一步。"
    "只寫紀錄裡有的事實，提到檔案或 commit 時保留名稱，總長不超過 500 字，不要開場白。"
)
MAX_INPUT_CHARS = 24000


class Summarizer:
    def __init__(self, hub: Hub, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.hub = hub
        self.cfg = cfg
        self._transport = transport  # 測試用
        self._running: set[tuple[str, str]] = set()
        self._loop_task: asyncio.Task | None = None

    def on_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "item" and event["kind"] == "commit":
            self.request(event["author"], event["project_id"], delay=5)

    def request(self, agent: str, project_id: str, delay: float = 0) -> None:
        key = (agent, project_id)
        if key in self._running:
            return
        self._running.add(key)
        asyncio.get_running_loop().create_task(self._run(key, delay))

    def start(self) -> None:
        self._loop_task = asyncio.get_running_loop().create_task(self._idle_loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            for agent, project_id in self.hub.summary_candidates():
                self.request(agent, project_id)

    async def _run(self, key: tuple[str, str], delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            await self.summarize(*key)
        except Exception:
            log.exception("摘要失敗：%s", key)
        finally:
            self._running.discard(key)

    async def summarize(self, agent: str, project_id: str) -> int | None:
        prev, items = self.hub.summary_input(agent, project_id)
        if not items:
            return None
        text = None
        if self.cfg.llm_api_key:
            try:
                _, text = await self.complete(_prompt(agent, prev, items))
            except LLMError as e:  # 全部模型都失敗（限流、下架…），退回條列就好
                log.warning("LLM 摘要失敗，改用條列：%s", e)
        return self.hub.save_summary(agent, project_id, text or fallback_summary(items), items[-1]["id"])

    async def complete(self, user: str) -> tuple[str, str]:
        """依序嘗試設定的模型，回傳 (成功的模型, 內容)。"""
        errors = []
        async with httpx.AsyncClient(timeout=300, transport=self._transport) as client:  # NVIDIA 免費方案常要排隊一兩分鐘
            for model in self.cfg.llm_models:
                try:
                    resp = await client.post(
                        f"{self.cfg.llm_base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.cfg.llm_api_key}"},
                        json={
                            "model": model,
                            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                         {"role": "user", "content": user}],
                            "temperature": 0.2,
                            "max_tokens": 2000,
                        },
                    )
                    if resp.status_code != 200:
                        # NVIDIA 的錯誤內容通常會說明原因（模型不存在、額度用完…）
                        errors.append(f"{model}：HTTP {resp.status_code} {resp.text[:200]}")
                        continue
                    content = resp.json()["choices"][0]["message"].get("content") or ""
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                    errors.append(f"{model}：{e}")
                    continue
                # 有些推理模型會把思考過程放在 <think> 裡
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
                if content:
                    return model, content
                errors.append(f"{model}：回傳空白")
        raise LLMError("；".join(errors) or "沒有設定任何模型")


class LLMError(Exception):
    pass


def _prompt(agent: str, prev: str | None, items: list[dict]) -> str:
    log_text = "\n\n".join(
        f"[commit {i['meta'].get('sha', '')[:7]}] {i['body']}" if i["kind"] == "commit" else i["body"]
        for i in items
    )[-MAX_INPUT_CHARS:]
    return (f"上一份進度摘要：\n{prev}\n\n" if prev else "") + f"{agent} 最新的工作紀錄：\n{log_text}"


def fallback_summary(items: list[dict]) -> str:
    prompts, files, commits = [], [], []
    for i in items:
        if i["kind"] == "commit":
            commits.append(f"{i['meta'].get('sha', '')[:7]} {i['body']}")
            continue
        for line in i["body"].splitlines():
            if line.startswith("使用者："):
                prompts.append(line.removeprefix("使用者：")[:120])
        files += i["meta"].get("files", [])
    lines = []
    if prompts:
        lines.append("最近的指令：" + "；".join(prompts[-5:]))
    if files:
        lines.append("改動的檔案：" + "、".join(dict.fromkeys(files[-15:])))
    if commits:
        lines.append("commit：" + "；".join(commits[-5:]))
    return "\n".join(f"- {line}" for line in lines) or "- （沒有可摘要的內容）"

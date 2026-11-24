"""AUN 設定：全部來自環境變數，方便放進 docker-compose 的 .env。"""

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if not value else value.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    db_path: str
    host: str
    port: int
    # 搭檔的進度（commit、摘要）最快多久附一次到 context
    progress_interval: float
    # 閒置多久後自動產生進度摘要
    summary_idle: float
    # OpenAI 相容的摘要模型（預設 NVIDIA build.nvidia.com）
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    discord_token: str
    discord_post_summaries: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=_env("AUN_DB", "/data/aun.db"),
            host=_env("AUN_HOST", "0.0.0.0"),
            port=int(_env("AUN_PORT", "8080")),
            progress_interval=float(_env("AUN_PROGRESS_INTERVAL_MINUTES", "30")) * 60,
            summary_idle=float(_env("AUN_SUMMARY_IDLE_MINUTES", "20")) * 60,
            llm_base_url=_env("AUN_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/"),
            llm_api_key=_env("AUN_LLM_API_KEY"),
            llm_model=_env("AUN_LLM_MODEL", "meta/llama-3.3-70b-instruct"),
            discord_token=_env("AUN_DISCORD_TOKEN"),
            discord_post_summaries=_bool("AUN_DISCORD_POST_SUMMARIES", True),
        )

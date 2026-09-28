"""Environment-backed settings for the QQ bot process."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load simple ``KEY=VALUE`` entries without adding a dotenv dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True, slots=True)
class QqBotSettings:
    """Credentials and runtime policy for the official QQ bot SDK."""

    appid: str
    secret: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    system_prompt: str
    max_history_messages: int
    max_reply_chars: int
    request_timeout_seconds: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> QqBotSettings:
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"missing required environment variable: {name}")
            return value

        def positive_int(name: str, default: int) -> int:
            value = int(env.get(name, str(default)))
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
            return value

        timeout = float(env.get("JARVIS_QQBOT_REQUEST_TIMEOUT", "60"))
        if timeout <= 0:
            raise ValueError("JARVIS_QQBOT_REQUEST_TIMEOUT must be > 0")

        return cls(
            appid=required("QQBOT_APPID"),
            secret=required("QQBOT_APPSECRET"),
            llm_base_url=env.get("QQBOT_LLM_BASE_URL", "https://api.openai.com/v1")
            .strip()
            .rstrip("/"),
            llm_model=env.get("QQBOT_LLM_MODEL", "gpt-4o-mini").strip(),
            llm_api_key=required("QQBOT_LLM_API_KEY"),
            system_prompt=env.get(
                "QQBOT_SYSTEM_PROMPT",
                "你是一个可靠、简洁的 QQ 助手。使用中文回答，除非用户要求其他语言。",
            ).strip(),
            max_history_messages=positive_int("QQBOT_MAX_HISTORY", 12),
            max_reply_chars=positive_int("QQBOT_MAX_REPLY_CHARS", 1800),
            request_timeout_seconds=timeout,
        )

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    admin_user_ids: str = ""

    database_url: str

    stt_provider: Literal["groq", "local_whisper"] = "groq"
    groq_api_key: str = ""

    llm_provider: Literal["gemini", "ollama"] = "gemini"
    gemini_api_key: str = ""

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:12b"

    telegram_webhook_base: str = ""
    telegram_webhook_secret: str = ""
    port: int = 8080

    # Network timeouts (seconds). Telegram's library defaults (5s read / 1s pool)
    # are too tight for downloading voice files from the CDN over Railway egress.
    telegram_connect_timeout: float = 15.0
    telegram_read_timeout: float = 60.0
    telegram_write_timeout: float = 60.0
    telegram_pool_timeout: float = 10.0
    # How long to wait on the Groq transcription HTTP call before giving up.
    stt_timeout: float = 120.0

    @property
    def admin_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_user_ids.split(",") if x.strip()}

    @property
    def use_webhook(self) -> bool:
        return bool(self.telegram_webhook_base and self.telegram_webhook_secret)


settings = Settings()  # type: ignore[call-arg]

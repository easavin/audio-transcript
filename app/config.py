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

    # Public base URL of the deployment, e.g. https://your-app.up.railway.app
    # Used for BOTH the Telegram and WhatsApp webhooks. Leave empty locally to
    # run Telegram in polling mode (WhatsApp still needs a public URL / tunnel).
    telegram_webhook_base: str = ""
    # Random secret — used as the Telegram webhook URL path and the
    # X-Telegram-Bot-Api-Secret-Token header.
    telegram_webhook_secret: str = ""
    port: int = 8080

    # --- WhatsApp Cloud API ---
    # The verify token YOU invent and paste into Meta's webhook config.
    whatsapp_verify_token: str = ""
    # Meta App Secret — used to validate X-Hub-Signature-256 on inbound events.
    whatsapp_app_secret: str = ""
    # Permanent access token (System User token) for the Graph API.
    whatsapp_access_token: str = ""
    # The phone number ID that sends messages (NOT the display phone number).
    whatsapp_phone_number_id: str = ""
    # Comma-separated admin WhatsApp numbers in wa_id form (digits only, no +).
    whatsapp_admin_ids: str = ""
    graph_api_version: str = "v21.0"

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
    def whatsapp_admin_set(self) -> set[str]:
        return {x.strip() for x in self.whatsapp_admin_ids.split(",") if x.strip()}

    @property
    def use_webhook(self) -> bool:
        return bool(self.telegram_webhook_base and self.telegram_webhook_secret)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(
            self.whatsapp_access_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_verify_token
        )


settings = Settings()  # type: ignore[call-arg]

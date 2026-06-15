from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from groq import APIConnectionError, APITimeoutError, AsyncGroq

from app.config import settings

log = logging.getLogger(__name__)


class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str: ...


class GroqSTT(STTProvider):
    """Groq-hosted whisper-large-v3-turbo. ~$0.04/hr, ~164x realtime."""

    MODEL = "whisper-large-v3-turbo"
    MAX_ATTEMPTS = 3

    def __init__(self, api_key: str, timeout: float = 120.0) -> None:
        # Disable the SDK's own retries; we retry explicitly below so timeouts and
        # transient connection errors get a few well-spaced attempts with logging.
        self._client = AsyncGroq(api_key=api_key, timeout=timeout, max_retries=0)

    async def transcribe(self, audio_path: Path) -> str:
        data = audio_path.read_bytes()
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                result = await self._client.audio.transcriptions.create(
                    file=(audio_path.name, data),
                    model=self.MODEL,
                )
                return result.text.strip()
            except (APITimeoutError, APIConnectionError) as exc:
                last_exc = exc
                log.warning(
                    "Groq STT attempt %d/%d failed: %s", attempt, self.MAX_ATTEMPTS, exc
                )
                if attempt < self.MAX_ATTEMPTS:
                    await asyncio.sleep(1.5 * attempt)
        assert last_exc is not None
        raise last_exc


class LocalWhisperSTT(STTProvider):
    """Placeholder — implemented in Phase 1.5 using faster-whisper on Mac Mini."""

    async def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError("Local Whisper provider not implemented yet")


def get_stt_provider() -> STTProvider:
    if settings.stt_provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required when STT_PROVIDER=groq")
        return GroqSTT(settings.groq_api_key, timeout=settings.stt_timeout)
    if settings.stt_provider == "local_whisper":
        return LocalWhisperSTT()
    raise RuntimeError(f"Unknown STT_PROVIDER: {settings.stt_provider}")

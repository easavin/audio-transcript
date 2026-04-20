from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from groq import AsyncGroq

from app.config import settings


class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str: ...


class GroqSTT(STTProvider):
    """Groq-hosted whisper-large-v3-turbo. ~$0.04/hr, ~164x realtime."""

    MODEL = "whisper-large-v3-turbo"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncGroq(api_key=api_key)

    async def transcribe(self, audio_path: Path) -> str:
        with audio_path.open("rb") as f:
            result = await self._client.audio.transcriptions.create(
                file=(audio_path.name, f.read()),
                model=self.MODEL,
            )
        return result.text.strip()


class LocalWhisperSTT(STTProvider):
    """Placeholder — implemented in Phase 1.5 using faster-whisper on Mac Mini."""

    async def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError("Local Whisper provider not implemented yet")


def get_stt_provider() -> STTProvider:
    if settings.stt_provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required when STT_PROVIDER=groq")
        return GroqSTT(settings.groq_api_key)
    if settings.stt_provider == "local_whisper":
        return LocalWhisperSTT()
    raise RuntimeError(f"Unknown STT_PROVIDER: {settings.stt_provider}")

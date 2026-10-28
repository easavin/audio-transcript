"""Platform-agnostic business logic shared by the Telegram and WhatsApp adapters.

Nothing in here knows about Telegram or WhatsApp message objects — adapters pass
in a `UserSettings` plus a transcript and get back text to send, and use `Batcher`
to coalesce voices that arrive in quick succession into one summary.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app import db
from app.prompts import build_summary_prompt, build_translation_prompt
from app.providers.llm import get_llm_provider

log = logging.getLogger(__name__)

MODES = ("transcript", "short", "medium", "full")
LANGS = ("auto", "ru", "en", "es")
LANG_LABELS = {"auto": "🌐 Auto", "ru": "🇷🇺 Russian", "en": "🇬🇧 English", "es": "🇪🇸 Spanish"}
MODE_LABELS = {
    "transcript": "📝 Transcript",
    "short": "⚡ Short",
    "medium": "📋 Medium",
    "full": "📖 Full",
}

# When several voices arrive within this window they are combined into one reply.
BATCH_WINDOW_SECONDS = 3.0
# Conservative chunk size that fits both Telegram (4096) and WhatsApp (4096) limits.
MAX_MSG_CHARS = 4000


async def transcript_to_reply(s: db.UserSettings, transcript: str) -> str:
    """Turn a (possibly combined) transcript into the text the user should receive.

    Raises on provider failure — adapters decide how to fall back.
    """
    if s.default_mode == "transcript":
        if s.default_output_lang == "auto":
            return transcript
        prompt = build_translation_prompt(transcript, s.default_output_lang)
        return await get_llm_provider().summarize(prompt)

    prompt = build_summary_prompt(transcript, s.default_mode, s.default_output_lang)
    return await get_llm_provider().summarize(prompt)


def chunk(text: str, size: int = MAX_MSG_CHARS) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


# ---------- Batching ----------

Flusher = Callable[[Any, db.UserSettings, str], Awaitable[None]]


@dataclass
class _Batch:
    settings: db.UserSettings
    payload: Any
    transcripts: list[str] = field(default_factory=list)
    flush_task: asyncio.Task | None = None


class Batcher:
    """Coalesces transcripts per conversation key, flushing after a quiet window.

    `flusher(payload, settings, combined_text)` is called once the user stops
    sending voices. `payload` is whatever the adapter needs to reply (e.g. a
    Telegram Update, or a WhatsApp recipient id).
    """

    def __init__(self, flusher: Flusher, window: float = BATCH_WINDOW_SECONDS) -> None:
        self._flusher = flusher
        self._window = window
        self._batches: dict[str, _Batch] = {}

    def add(self, key: str, s: db.UserSettings, transcript: str, payload: Any) -> None:
        batch = self._batches.get(key)
        if batch is None:
            batch = _Batch(settings=s, payload=payload)
            self._batches[key] = batch

        batch.transcripts.append(transcript)
        batch.settings = s  # user may have changed mode/lang between voices
        batch.payload = payload

        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        batch.flush_task = asyncio.create_task(self._flush_after(key))

    async def _flush_after(self, key: str) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return

        batch = self._batches.get(key)
        if batch is None or batch.flush_task is not asyncio.current_task():
            return  # a newer voice arrived and rescheduled the flush
        self._batches.pop(key, None)

        if not batch.transcripts:
            return
        combined = "\n\n".join(batch.transcripts)
        try:
            await self._flusher(batch.payload, batch.settings, combined)
        except Exception:
            log.exception("Batch flush failed for key=%s", key)

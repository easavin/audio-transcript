from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

import anthropic
import httpx
from google import genai

from app.config import settings


class LLMProvider(ABC):
    @abstractmethod
    async def summarize(self, prompt: str) -> str: ...


class AnthropicLLM(LLMProvider):
    MODEL = "claude-sonnet-5-5"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def summarize(self, prompt: str) -> str:
        resp = await self._client.beta.messages.create(
            model=self.MODEL,
            max_tokens=16000,
            # Summaries and translations don't need deep reasoning; low effort
            # keeps latency and token spend down.
            output_config={"effort": "low"},
            # If a safety classifier declines, retry server-side on the
            # recommended fallback model instead of failing the request.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined the request: {resp.stop_details}")
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class GeminiLLM(LLMProvider):
    MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def summarize(self, prompt: str) -> str:
        resp = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
        )
        return (resp.text or "").strip()


class OllamaLLM(LLMProvider):
    """Local Ollama, e.g. gemma3:12b on Mac Mini M4."""

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def summarize(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self._base_url}/api/generate",
                json={"model": self._model, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
            return r.json()["response"].strip()


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Built once and reused, so the HTTP client's connection pool is shared."""
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        return AnthropicLLM(settings.anthropic_api_key)
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiLLM(settings.gemini_api_key)
    if settings.llm_provider == "ollama":
        return OllamaLLM(settings.ollama_base_url, settings.ollama_model)
    raise RuntimeError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")

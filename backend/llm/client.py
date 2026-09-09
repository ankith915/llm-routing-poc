"""Async client for OpenAI-compatible chat completions APIs (Groq, OpenAI, OpenRouter).

Every supported provider speaks the same /chat/completions dialect, so one
client covers all of them; only auth headers and a few per-provider request
fields differ.
"""
import asyncio
import time
from dataclasses import dataclass

import httpx


@dataclass
class LLMResponse:
    content: str
    model_used: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class LLMError(RuntimeError):
    pass


# Historical name - kept so existing imports and `except` clauses keep working.
OpenRouterError = LLMError

ENV_KEY_FOR = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class LLMClient:
    """Works against any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 120.0,
                 provider: str = "groq"):
        if not api_key:
            raise LLMError(
                f"API key for '{provider}' is not set. Copy .env.example to .env and fill it in."
            )
        self.provider = provider
        headers = {"Authorization": f"Bearer {api_key}"}
        if provider == "openrouter":
            headers |= {"HTTP-Referer": "http://localhost:8000", "X-Title": "llm-routing-poc"}
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)

    async def chat(self, model: str, messages: list, temperature: float = 0.2,
                   max_tokens: int = 700, retries: int = 3,
                   extra_params: dict | None = None) -> LLMResponse:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **(extra_params or {}),
        }
        if self.provider == "openrouter":
            payload["usage"] = {"include": True}
        last_err = None
        for attempt in range(retries + 1):
            start = time.perf_counter()
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                latency_ms = int((time.perf_counter() - start) * 1000)
                if resp.status_code == 429:
                    # Groq's free tier is 8k tokens/min - back off hard.
                    raise LLMError(f"HTTP 429 (rate limited): {resp.text[:150]}")
                if resp.status_code in (500, 502, 503):
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise LLMError(str(data["error"])[:300])
                choice = data["choices"][0]
                usage = data.get("usage", {})
                content = choice["message"].get("content") or ""
                # Reasoning models (Groq's gpt-oss family) can spend the whole
                # completion budget on hidden reasoning and return nothing at
                # all. That is a failed call, not an answer worth grading.
                if not content.strip():
                    raise LLMError(
                        f"{model} returned an empty completion "
                        f"(finish_reason={choice.get('finish_reason')}, "
                        f"reasoning_tokens="
                        f"{usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)})"
                    )
                return LLMResponse(
                    content=content,
                    model_used=data.get("model", model),
                    provider=data.get("provider", self.provider),
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                    latency_ms=latency_ms,
                )
            except (httpx.HTTPError, LLMError, KeyError) as e:
                last_err = e
                if attempt < retries:
                    wait = 12.0 if "429" in str(e) else 1.5 * (attempt + 1)
                    await asyncio.sleep(wait)
        raise LLMError(f"LLM call failed for {model} ({self.provider}): {last_err}")

    async def aclose(self):
        await self._client.aclose()


# Historical name - kept so existing imports keep working.
OpenRouterClient = LLMClient


class ClientPool:
    """One client per provider; raises a clear error for unconfigured providers."""

    def __init__(self, clients: dict):
        self._clients = clients

    @classmethod
    def from_settings(cls, settings) -> "ClientPool":
        clients = {}
        for provider, key in settings.api_keys.items():
            if key:
                clients[provider] = LLMClient(
                    key, settings.base_urls[provider], provider=provider)
        return cls(clients)

    @property
    def providers(self) -> list:
        return sorted(self._clients)

    def has(self, provider: str) -> bool:
        return provider in self._clients

    def get(self, provider: str) -> LLMClient:
        client = self._clients.get(provider)
        if client is None:
            env = ENV_KEY_FOR.get(provider, f"{provider.upper()}_API_KEY")
            raise LLMError(
                f"No API key configured for provider '{provider}'. Set {env} in .env."
            )
        return client

    async def aclose(self):
        for c in self._clients.values():
            await c.aclose()

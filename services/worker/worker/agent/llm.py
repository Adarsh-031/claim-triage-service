"""Provider-agnostic LLM interface (swap by config). Timeouts, retries, circuit breaker (A5)."""
from __future__ import annotations

import random
import time
from collections import deque
from typing import Protocol

import httpx
from triage_common.settings import settings


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, threshold: int, window_s: float, reset_s: float):
        self.threshold, self.window_s, self.reset_s = threshold, window_s, reset_s
        self.failures: deque[float] = deque()
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.time() - self.opened_at >= self.reset_s else "open"

    def before(self) -> None:
        if self.state == "open":
            raise CircuitOpen("provider circuit open")

    def success(self) -> None:
        self.failures.clear()
        self.opened_at = None

    def failure(self) -> None:
        now = time.time()
        self.failures.append(now)
        while self.failures and now - self.failures[0] > self.window_s:
            self.failures.popleft()
        if self.state == "half_open" or len(self.failures) >= self.threshold:
            self.opened_at = now


class OpenAICompatibleLLM:
    """HTTP client for an OpenAI-compatible chat-completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(
            base_url=base_url,
            timeout=settings.llm_timeout_s,
            headers=headers,
        )

    def chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        resp = self.client.post("/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        # Preserve the same message shape regardless of whether the endpoint is
        # the local stub or OpenRouter.
        msg = data["choices"][0]["message"]
        msg["usage"] = data.get("usage", {})
        return msg


class ResilientLLM:
    """primary -> (retries w/ backoff+jitter) -> breaker -> fallback model -> raise (caller escalates)."""

    def __init__(self, primary: LLM, fallback: LLM | None):
        self.primary, self.fallback = primary, fallback
        self.breaker = CircuitBreaker(settings.breaker_fail_threshold, settings.breaker_window_s,
                                      settings.breaker_reset_s)

    def chat(self, messages, tools=None):
        try:
            self.breaker.before()
            for attempt in range(settings.retry_max):
                try:
                    out = self.primary.chat(messages, tools)
                    self.breaker.success()
                    return out
                except (httpx.HTTPError, KeyError):
                    self.breaker.failure()
                    time.sleep(min(8, 0.5 * 2 ** attempt) + random.uniform(0, 0.3))
            raise CircuitOpen("primary exhausted retries")
        except CircuitOpen:
            if self.fallback is None:
                raise
            return self.fallback.chat(messages, tools)


def get_llm() -> LLM:
    if settings.llm_provider == "stub":
        # The default is a local HTTP service, so callers exercise the same
        # OpenAI-compatible request/response path as a real provider.
        return OpenAICompatibleLLM(settings.llm_base_url, settings.llm_model_primary or "stub")

    if settings.llm_provider != "openrouter":
        raise ValueError("LLM_PROVIDER must be 'stub' or 'openrouter'")
    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter")
    fallback = (
        OpenAICompatibleLLM(
            settings.openrouter_base_url,
            settings.llm_model_fallback,
            settings.openrouter_api_key,
        )
        if settings.llm_model_fallback
        else None
    )
    primary = OpenAICompatibleLLM(
        settings.openrouter_base_url,
        settings.llm_model_primary,
        settings.openrouter_api_key,
    )
    return ResilientLLM(primary, fallback)

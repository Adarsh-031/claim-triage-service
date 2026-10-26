"""Provider-agnostic LLM interface (swap by config). Timeouts, retries, circuit breaker (A5)."""
from __future__ import annotations

import random
import time
from typing import Protocol
from uuid import uuid4

import httpx
import redis
from triage_common.settings import settings

from ..metrics import LLM_BREAKER_STATE


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    """Redis-shared provider breaker; a single worker owns the half-open probe."""

    def __init__(self, threshold: int, window_s: float, reset_s: float, client=None, name: str = "primary"):
        self.threshold, self.window_s, self.reset_s = threshold, window_s, reset_s
        self.client = client or redis.Redis.from_url(settings.redis_url, decode_responses=True)
        self.name = name
        self.failures_key = f"llm:breaker:{name}:failures"
        self.open_key = f"llm:breaker:{name}:opened"
        self.probe_key = f"llm:breaker:{name}:probe"
        self.probe_token = None

    def _state(self) -> str:
        opened_at = self.client.get(self.open_key)
        if opened_at is None:
            return "closed"
        if time.time() - float(opened_at) < self.reset_s:
            return "open"
        return "half_open"

    @property
    def state(self) -> str:
        return self._state()

    def before(self) -> None:
        state = self._state()
        LLM_BREAKER_STATE.labels(self.name).set({"closed": 0, "open": 1, "half_open": 2}[state])
        if state == "open":
            raise CircuitOpen("provider circuit open")
        if state == "half_open":
            token = uuid4().hex
            acquired = self.client.set(self.probe_key, token, nx=True, px=max(1000, int(settings.llm_timeout_s * 1000)))
            if not acquired:
                raise CircuitOpen("provider circuit half-open probe already in progress")
            self.probe_token = token

    def success(self) -> None:
        self.client.delete(self.failures_key, self.open_key)
        self._release_probe()
        LLM_BREAKER_STATE.labels(self.name).set(0)

    def failure(self) -> None:
        now = time.time()
        pipeline = self.client.pipeline()
        pipeline.zremrangebyscore(self.failures_key, "-inf", now - self.window_s)
        pipeline.zadd(self.failures_key, {f"{now}:{uuid4().hex}": now})
        pipeline.expire(self.failures_key, max(1, int(self.window_s) + 1))
        pipeline.zcard(self.failures_key)
        count = pipeline.execute()[-1]
        if self.state == "half_open" or count >= self.threshold:
            self.client.set(self.open_key, str(now), ex=max(1, int(self.reset_s) + 3600))
        self._release_probe()
        LLM_BREAKER_STATE.labels(self.name).set(1 if self.state == "open" else 0)

    def _release_probe(self) -> None:
        if self.probe_token:
            self.client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                1, self.probe_key, self.probe_token,
            )
            self.probe_token = None


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

    def __init__(self, primary: LLM, fallback: LLM | None, breaker: CircuitBreaker | None = None):
        self.primary, self.fallback = primary, fallback
        self.breaker = breaker or CircuitBreaker(
            settings.breaker_fail_threshold, settings.breaker_window_s, settings.breaker_reset_s
        )

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
                    if attempt + 1 < settings.retry_max:
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

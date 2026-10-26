"""Provider-agnostic LLM interface (swap by config). Timeouts, retries, circuit breaker (A5)."""
from __future__ import annotations

import random
import time
import logging
from typing import Protocol
from uuid import uuid4

import httpx
import redis
from triage_common.settings import settings

from ..metrics import LLM_BREAKER_STATE

log = logging.getLogger(__name__)


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    """Redis-shared provider breaker; a single worker owns the half-open probe."""

    def __init__(self, threshold: int, window_s: float, reset_s: float, client=None, name: str = "primary"):
        self.threshold, self.window_s, self.reset_s = threshold, window_s, reset_s
        self.client = client or redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
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
        try:
            return self._state()
        except redis.RedisError:
            log.warning("Redis unavailable while reading %s circuit state; treating breaker as closed", self.name)
            return "closed"

    def before(self) -> None:
        probe_acquired = False
        try:
            state = self._state()
            if state == "half_open":
                token = uuid4().hex
                acquired = self.client.set(
                    self.probe_key, token, nx=True,
                    px=max(1000, int(settings.llm_timeout_s * 1000)),
                )
                if not acquired:
                    state = "open"
                else:
                    self.probe_token = token
                    probe_acquired = True
        except redis.RedisError:
            log.warning("Redis unavailable before %s call; treating breaker as closed", self.name)
            return
        LLM_BREAKER_STATE.labels(self.name).set({"closed": 0, "open": 1, "half_open": 2}[state])
        if state == "open":
            raise CircuitOpen("provider circuit open")
        if state == "half_open" and not probe_acquired:
            raise CircuitOpen("provider circuit half-open probe already in progress")

    def success(self) -> None:
        try:
            self.client.delete(self.failures_key, self.open_key)
        except redis.RedisError:
            log.warning("Redis unavailable after successful %s call; breaker state may be stale", self.name)
        finally:
            self._release_probe()
        LLM_BREAKER_STATE.labels(self.name).set(0)

    def failure(self) -> None:
        try:
            now = time.time()
            state = self._state()
            pipeline = self.client.pipeline()
            pipeline.zremrangebyscore(self.failures_key, "-inf", now - self.window_s)
            pipeline.zadd(self.failures_key, {f"{now}:{uuid4().hex}": now})
            pipeline.expire(self.failures_key, max(1, int(self.window_s) + 1))
            pipeline.zcard(self.failures_key)
            count = pipeline.execute()[-1]
            if state == "half_open":
                self.client.set(self.open_key, str(now), ex=max(1, int(self.reset_s) + 3600))
            elif state == "closed" and count >= self.threshold:
                self.client.set(self.open_key, str(now), ex=max(1, int(self.reset_s) + 3600), nx=True)
        except redis.RedisError:
            log.warning("Redis unavailable after failed %s call; treating breaker as closed", self.name)
        finally:
            self._release_probe()
        state = self.state
        LLM_BREAKER_STATE.labels(self.name).set({"closed": 0, "open": 1, "half_open": 2}[state])

    def _release_probe(self) -> None:
        if self.probe_token:
            try:
                self.client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                    1, self.probe_key, self.probe_token,
                )
            except redis.RedisError:
                log.warning("Redis unavailable while releasing %s half-open probe", self.name)
            finally:
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

    def __init__(self, primary: LLM, fallback: LLM | None, breaker: CircuitBreaker | None = None,
                 provider_name: str | None = None):
        self.primary, self.fallback = primary, fallback
        self.breaker = breaker or CircuitBreaker(
            settings.breaker_fail_threshold, settings.breaker_window_s, settings.breaker_reset_s,
            name=provider_name or settings.llm_provider,
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
                    if self.breaker.state != "closed":
                        break
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
    return ResilientLLM(primary, fallback, provider_name=settings.llm_provider)

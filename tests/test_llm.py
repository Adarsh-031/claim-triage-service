from __future__ import annotations

import time

import httpx
import pytest
import redis
from prometheus_client import generate_latest
from worker.agent.llm import CircuitBreaker, CircuitOpen, ResilientLLM, get_llm
from worker.metrics import LLM_BREAKER_STATE


class FakePipeline:
    def __init__(self, owner):
        self.owner = owner
        self.actions = []

    def zremrangebyscore(self, key, _minimum, maximum):
        self.actions.append(("trim", key, float(maximum)))
        return self

    def zadd(self, key, values):
        self.actions.append(("add", key, values))
        return self

    def expire(self, key, _seconds):
        self.actions.append(("expire", key))
        return self

    def zcard(self, key):
        self.actions.append(("count", key))
        return self

    def execute(self):
        for action in self.actions:
            kind, key, *args = action
            if kind == "trim":
                self.owner.sorted[key] = {member: score for member, score in self.owner.sorted.get(key, {}).items()
                                          if score >= args[0]}
            elif kind == "add":
                additions = {member: float(score) for member, score in args[0].items()}
                self.owner.sorted.setdefault(key, {}).update(additions)
            elif kind == "count":
                count = len(self.owner.sorted.get(key, {}))
        return [None] * (len(self.actions) - 1) + [count]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sorted = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, **_kwargs):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.sorted.pop(key, None)

    def pipeline(self):
        return FakePipeline(self)

    def eval(self, _script, key_count, *args):
        if key_count == 3:
            probe_key, failures_key, open_key, token = args
            if self.values.get(probe_key) == token:
                self.values.pop(probe_key, None)
                self.values.pop(failures_key, None)
                self.values.pop(open_key, None)
                self.sorted.pop(failures_key, None)
            return
        key, token = args
        if self.values.get(key) == token:
            self.values.pop(key, None)


class StubLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, _messages, _tools=None, *, timeout_s=None):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_breaker_opens_after_threshold_and_rejects_calls(monkeypatch):
    client = FakeRedis()
    breaker = CircuitBreaker(2, 30, 30, client=client)
    breaker.failure()
    assert breaker.state == "closed"
    breaker.failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen, match="open"):
        breaker.before()


def test_breaker_metric_reads_shared_state_at_scrape_time():
    client = FakeRedis()
    breaker = CircuitBreaker(1, 30, 30, client=client, name="scrape-test")

    def metric_value():
        lines = generate_latest(LLM_BREAKER_STATE).decode().splitlines()
        sample = next(line for line in lines if 'provider="scrape-test"' in line)
        return float(sample.rsplit(" ", 1)[1])

    assert metric_value() == 0
    client.values[breaker.open_key] = str(time.time())
    assert metric_value() == 1
    client.values[breaker.open_key] = str(time.time() - 31)
    assert metric_value() == 2


def test_breaker_does_not_reset_open_window_on_later_failure():
    client = FakeRedis()
    breaker = CircuitBreaker(1, 30, 30, client=client)
    breaker.failure()
    opened_at = client.values[breaker.open_key]

    breaker.failure()

    assert client.values[breaker.open_key] == opened_at


def test_breaker_counts_failures_across_successful_requests():
    client = FakeRedis()
    breaker = CircuitBreaker(2, 30, 30, client=client)

    breaker.failure()
    breaker.success()  # Ordinary success must not erase the sliding failure window.
    breaker.failure()

    assert breaker.state == "open"


def test_in_flight_closed_request_cannot_close_breaker_opened_elsewhere():
    client = FakeRedis()
    breaker = CircuitBreaker(1, 30, 30, client=client)

    breaker.failure()
    breaker.success()

    assert breaker.state == "open"


def test_breaker_allows_one_half_open_probe_and_closes_after_success(monkeypatch):
    client = FakeRedis()
    breaker = CircuitBreaker(1, 30, 30, client=client)
    breaker.failure()
    client.values[breaker.open_key] = str(time.time() - 31)
    assert breaker.state == "half_open"
    breaker.before()
    with pytest.raises(CircuitOpen, match="probe"):
        CircuitBreaker(1, 30, 30, client=client).before()
    breaker.success()
    assert breaker.state == "closed"
    assert breaker.failures_key not in client.sorted
    assert breaker.probe_key not in client.values


def test_resilient_llm_retries_primary_then_uses_fallback(monkeypatch):
    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 3)
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda _delay: None)
    monkeypatch.setattr("worker.agent.llm.random.uniform", lambda *_args: 0)
    primary = StubLLM([httpx.ConnectError("offline"), httpx.ConnectError("offline"), httpx.ConnectError("offline")])
    fallback = StubLLM([{"content": "fallback"}])
    resilient = ResilientLLM(primary, fallback, CircuitBreaker(5, 30, 30, client=FakeRedis()))

    assert resilient.chat([])["content"] == "fallback"
    assert primary.calls == 3
    assert fallback.calls == 1


def test_resilient_llm_stops_retries_when_breaker_opens(monkeypatch):
    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 5)
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda _delay: None)
    primary = StubLLM([httpx.ConnectError("offline") for _ in range(5)])
    fallback = StubLLM([{"content": "fallback"}])
    breaker = CircuitBreaker(1, 30, 30, client=FakeRedis())

    assert ResilientLLM(primary, fallback, breaker).chat([])["content"] == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_redis_errors_do_not_interrupt_provider_fallback(monkeypatch):
    class UnavailableRedis(FakeRedis):
        def get(self, _key):
            raise redis.RedisError("redis unavailable")

        def delete(self, *_keys):
            raise redis.RedisError("redis unavailable")

        def pipeline(self):
            raise redis.RedisError("redis unavailable")

    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 2)
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda _delay: None)
    primary = StubLLM([httpx.ConnectError("provider offline") for _ in range(2)])
    fallback = StubLLM([{"content": "fallback"}])
    breaker = CircuitBreaker(1, 30, 30, client=UnavailableRedis())

    assert ResilientLLM(primary, fallback, breaker).chat([])["content"] == "fallback"
    assert primary.calls == 2
    assert fallback.calls == 1


def test_redis_errors_during_half_open_probe_and_release_are_isolated():
    class UnavailableProbeRedis(FakeRedis):
        def get(self, key):
            if key.endswith(":opened"):
                return str(time.time() - 31)
            return super().get(key)

        def set(self, *_args, **_kwargs):
            raise redis.RedisError("redis unavailable")

        def eval(self, *_args):
            raise redis.RedisError("redis unavailable")

    breaker = CircuitBreaker(1, 30, 30, client=UnavailableProbeRedis())
    breaker.probe_token = "expired-probe-token"

    breaker.before()
    breaker.success()


def test_stub_provider_uses_resilience_and_provider_label(monkeypatch):
    monkeypatch.setattr("worker.agent.llm.settings.llm_provider", "stub")

    resilient = get_llm()

    assert isinstance(resilient, ResilientLLM)
    assert resilient.breaker.name == "stub"


def test_resilient_llm_breaker_uses_configured_provider_name(monkeypatch):
    monkeypatch.setattr("worker.agent.llm.settings.llm_provider", "openrouter")

    resilient = ResilientLLM(StubLLM([]), None, provider_name="openrouter")

    assert resilient.breaker.name == "openrouter"


def test_resilient_llm_uses_primary_without_fallback_when_primary_recovers(monkeypatch):
    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 3)
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda _delay: None)
    primary = StubLLM([httpx.ConnectError("temporary"), {"content": "primary"}])
    fallback = StubLLM([{"content": "fallback"}])
    resilient = ResilientLLM(primary, fallback, CircuitBreaker(5, 30, 30, client=FakeRedis()))

    assert resilient.chat([])["content"] == "primary"
    assert fallback.calls == 0


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_resilient_llm_does_not_retry_non_retryable_http_status(monkeypatch, status):
    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 4)
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.test/chat"))
    error = httpx.HTTPStatusError("provider rejected request", request=response.request, response=response)
    primary = StubLLM([error])
    fallback = StubLLM([{"content": "fallback"}])
    breaker = CircuitBreaker(5, 30, 30, client=FakeRedis())

    result = ResilientLLM(primary, fallback, breaker).chat([])

    assert result["content"] == "fallback"
    assert primary.calls == fallback.calls == 1
    assert breaker.state == "closed"
    assert not breaker.client.sorted.get(breaker.failures_key)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_resilient_llm_retries_transient_http_status(monkeypatch, status):
    monkeypatch.setattr("worker.agent.llm.settings.retry_max", 3)
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda _delay: None)
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.test/chat"))
    error = httpx.HTTPStatusError("transient provider response", request=response.request, response=response)
    primary = StubLLM([error, error, {"content": "recovered"}])
    resilient = ResilientLLM(primary, None, CircuitBreaker(5, 30, 30, client=FakeRedis()))

    assert resilient.chat([])["content"] == "recovered"
    assert primary.calls == 3


def test_resilient_llm_bounds_retries_to_remaining_run_deadline(monkeypatch):
    now = [100.0]
    seen_timeouts = []
    monkeypatch.setattr("worker.agent.llm.time.monotonic", lambda: now[0])
    monkeypatch.setattr("worker.agent.llm.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr("worker.agent.llm.random.uniform", lambda *_args: 0)

    class DeadlineProvider:
        def chat(self, _messages, _tools=None, *, timeout_s=None):
            seen_timeouts.append(timeout_s)
            raise httpx.ConnectError("offline")

    resilient = ResilientLLM(DeadlineProvider(), None, CircuitBreaker(5, 30, 30, client=FakeRedis()))
    with pytest.raises(httpx.TimeoutException, match="deadline exhausted"):
        resilient.chat([], timeout_s=0.75)

    assert seen_timeouts == [0.75, 0.25]

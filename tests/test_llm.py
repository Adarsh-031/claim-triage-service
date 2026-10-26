from __future__ import annotations

import time

import httpx
import pytest
import redis
from worker.agent.llm import CircuitBreaker, CircuitOpen, ResilientLLM


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

    def eval(self, _script, _key_count, key, token):
        if self.values.get(key) == token:
            self.values.pop(key, None)


class StubLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, _messages, _tools=None):
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


def test_breaker_does_not_reset_open_window_on_later_failure():
    client = FakeRedis()
    breaker = CircuitBreaker(1, 30, 30, client=client)
    breaker.failure()
    opened_at = client.values[breaker.open_key]

    breaker.failure()

    assert client.values[breaker.open_key] == opened_at


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

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis
from app import main as api
from fastapi.testclient import TestClient
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(api.app)


class FakeSession:
    def __init__(self, added):
        self.added = added

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return self

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if isinstance(obj, api.Quarantine) and obj.received_at is None:
                obj.received_at = datetime.now(UTC)


class FakeRedis:
    def __init__(self):
        self.lpush_calls = []
        self.eval_calls = 0
        self.values = {}

    def eval(self, _script, _count, _key, limit, _refill):
        self.eval_calls += 1
        return [int(self.eval_calls <= limit), 0 if self.eval_calls <= limit else 6]

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kwargs):
        self.values[key] = value

    def lpush(self, *args):
        self.lpush_calls.append(args)


def demo_claim():
    scenario = json.loads(
        (ROOT / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text(encoding="utf-8")
    )
    return scenario["claim"]


def test_repeated_claim_is_returned_from_client_scoped_run_cache(monkeypatch):
    stored = []
    fake_redis = FakeRedis()
    claim_data = demo_claim()
    client_id = "127.0.0.1"
    prompt_version = "v1"
    cached_result = {
        "route": "escalate",
        "summary": "Cached safe result",
        "entitlements": [],
        "compensation_inr": None,
        "citations": [],
        "escalation_reason": "VERIFICATION_FAILED",
        "verified": False,
        "requires_human_approval": True,
    }
    normalized_claim = api.ClaimIn.model_validate(claim_data).model_dump(mode="json")
    key = api.run_cache_key(normalized_claim, prompt_version,
                            api.current_corpus_version(fake_redis), client_id)
    fake_redis.set(key, json.dumps({"status": "escalated", "result": cached_result}), ex=300)
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)
    request = Request({
        "type": "http", "method": "POST", "path": "/v1/claims",
        "headers": [(b"x-client-id", b"forged-other-client")],
        "client": ("127.0.0.1", 12345), "state": {"cid": "cache-test"},
    })

    response = api.submit_claim(api.ClaimIn.model_validate(claim_data), request)

    assert response.status == api.RunStatus.escalated
    assert len(stored) == 1
    assert stored[0].result == cached_result
    assert stored[0].trace[0]["hit"] is True
    assert fake_redis.lpush_calls == []


def test_worker_cache_write_is_reused_by_api_without_enqueue(monkeypatch):
    from triage_common.contracts import EscalationReason, Route
    from worker import main as worker

    stored = []
    fake_redis = FakeRedis()
    monkeypatch.setattr(api.settings, "cache_enabled", True)
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)
    monkeypatch.setattr(worker, "cache_redis", fake_redis)
    monkeypatch.setattr(worker, "get_run", lambda _run_id: stored[0])

    def update_run(run_id, **fields):
        run = next(item for item in stored if getattr(item, "id", None) == run_id)
        for name, value in fields.items():
            setattr(run, name, value)

    monkeypatch.setattr(worker, "update_run", update_run)
    monkeypatch.setattr(
        worker,
        "run_agent",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                route=Route.ESCALATE,
                summary="Needs officer review",
                amount=None,
                citations=[],
                escalation_reason=EscalationReason.VERIFICATION_FAILED,
            ),
            [{"step": "finalize"}],
        ),
    )

    claim = api.ClaimIn.model_validate(demo_claim())

    def request():
        return Request({
            "type": "http", "method": "POST", "path": "/v1/claims", "headers": [],
            "client": ("127.0.0.1", 12345), "state": {"cid": "cache-roundtrip"},
        })

    first = api.submit_claim(claim, request())
    job = json.loads(fake_redis.lpush_calls[0][1])
    worker.handle(job)
    second = api.submit_claim(claim, request())

    assert first.status is api.RunStatus.queued
    assert second.status is api.RunStatus.escalated
    assert len(fake_redis.lpush_calls) == 1
    assert len(stored) == 2
    assert stored[0].claim == api.ClaimIn.model_validate(demo_claim()).model_dump(mode="json")
    assert "client_id" not in stored[0].claim
    assert "prompt_version" not in stored[0].claim
    assert stored[1].trace[0]["hit"] is True


def test_invalid_claim_is_quarantined_and_never_enqueued(monkeypatch):
    stored = []
    session = FakeSession(stored)
    fake_redis = FakeRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: session)
    monkeypatch.setattr(api, "r", fake_redis)

    claim = demo_claim()
    claim["statement"] += chr(1)
    response = client.post(
        "/v1/claims",
        json=claim,
        headers={"x-client-id": "quarantine-test-client"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["quarantine_id"]
    assert any(error["field"] == "statement" for error in payload["errors"])
    assert len(stored) == 1
    quarantine = stored[0]
    assert isinstance(quarantine, api.Quarantine)
    assert json.loads(quarantine.raw_body)["statement"] == claim["statement"]
    assert quarantine.client_id == "testclient"
    assert len(quarantine.raw_body.encode("utf-8")) <= api.RAW_BODY_LIMIT
    assert fake_redis.eval_calls == 1
    assert fake_redis.lpush_calls == []


def test_quarantine_body_storage_is_capped(monkeypatch):
    stored = []
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", FakeRedis())
    claim = demo_claim()
    claim["statement"] = "x" * 18_000

    response = client.post("/v1/claims", json=claim)

    assert response.status_code == 422
    assert len(stored) == 1
    assert len(stored[0].raw_body.encode("utf-8")) == api.RAW_BODY_LIMIT


def test_quarantine_endpoint_returns_latest_items_and_client_metadata(monkeypatch):
    record = SimpleNamespace(
        id=uuid.uuid4(),
        received_at=datetime(2026, 10, 25, tzinfo=UTC),
        raw_body='{"pnr":"bad"}',
        errors=[{"field": "pnr", "msg": "invalid format", "type": "string_pattern_mismatch"}],
        client_id="demo-client",
    )
    calls = []
    monkeypatch.setattr(api, "list_quarantine", lambda limit: calls.append(limit) or [record])

    response = client.get("/v1/quarantine?limit=1")

    assert response.status_code == 200
    assert calls == [1]
    item = response.json()[0]
    assert item["id"] == str(record.id)
    assert item["received_at"] == record.received_at.isoformat()
    assert item["raw_body"] == record.raw_body
    assert item["errors"] == record.errors
    assert item["client_id"] == "demo-client"


def test_valid_claim_still_queues_unchanged(monkeypatch):
    stored = []
    fake_redis = FakeRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)

    response = client.post("/v1/claims", json=demo_claim())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"]
    assert len(stored) == 1
    assert isinstance(stored[0], api.Run)
    assert "client_id" not in stored[0].claim
    assert "prompt_version" not in stored[0].claim
    assert fake_redis.eval_calls == 1
    assert len(fake_redis.lpush_calls) == 1
    assert fake_redis.lpush_calls[0][0] == api.settings.queue_name
    queued = json.loads(fake_redis.lpush_calls[0][1])
    assert queued["client_id"] == "testclient"
    assert queued["prompt_version"] == api.settings.prompt_version



def test_s02_malformed_quarantine_is_persisted_with_field_errors_without_queueing(monkeypatch):
    scenario = json.loads(
        (ROOT / "eval/scenarios/s02_malformed_quarantine.json").read_text(encoding="utf-8")
    )
    stored = []
    fake_redis = FakeRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)

    response = client.post(
        "/v1/claims",
        json=scenario["claim"],
        headers={"x-client-id": "s02-acceptance-test"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["quarantine_id"]
    assert any(error["field"] == "pnr" for error in payload["errors"])
    assert len(stored) == 1
    assert isinstance(stored[0], api.Quarantine)
    assert json.loads(stored[0].raw_body) == scenario["claim"]
    assert stored[0].client_id == "testclient"
    assert fake_redis.eval_calls == 1
    assert fake_redis.lpush_calls == []



def test_cross_field_validation_error_is_reported_against_destination(monkeypatch):
    stored = []
    fake_redis = FakeRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)
    claim = demo_claim()
    claim["destination"] = claim["origin"]

    response = client.post("/v1/claims", json=claim)

    assert response.status_code == 422
    assert any(error["field"] == "destination" for error in response.json()["errors"])
    assert len(stored) == 1
    assert isinstance(stored[0], api.Quarantine)
    assert fake_redis.eval_calls == 1
    assert fake_redis.lpush_calls == []



def test_quarantine_storage_limit_is_measured_in_utf8_bytes(monkeypatch):
    stored = []
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", FakeRedis())
    claim = demo_claim()
    claim["statement"] = "✈" * 7_000

    response = client.post("/v1/claims", json=claim)

    assert response.status_code == 422
    assert len(stored) == 1
    assert len(stored[0].raw_body.encode("utf-8")) <= api.RAW_BODY_LIMIT



def test_invalid_claims_are_rate_limited_before_quarantine(monkeypatch):
    stored = []
    fake_redis = FakeRedis()
    fake_redis.eval = lambda *_args: [0, 6]
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)

    response = client.post("/v1/claims", json={"pnr": "bad"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "6"
    assert stored == []
    assert fake_redis.lpush_calls == []


def test_token_bucket_allows_ten_and_rejects_the_eleventh():
    fake_redis = FakeRedis()
    decisions = [api.allow_run(fake_redis, "token-bucket-client", 10) for _ in range(11)]
    assert all(allowed for allowed, _ in decisions[:10])
    assert decisions[10] == (False, 6)


def test_rate_limit_returns_service_unavailable_when_redis_is_down(monkeypatch):
    class UnavailableRedis(FakeRedis):
        def eval(self, *_args):
            raise redis.RedisError("redis unavailable")

    stored = []
    fake_redis = UnavailableRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)

    request = Request({
        "type": "http", "method": "POST", "path": "/v1/claims", "headers": [],
        "client": ("203.0.113.17", 12345), "state": {"cid": "redis-outage"},
    })

    with pytest.raises(api.HTTPException) as exc_info:
        api.submit_claim(api.ClaimIn.model_validate(demo_claim()), request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "rate limiter unavailable"
    assert stored == []
    assert fake_redis.lpush_calls == []


def test_source_ip_is_used_instead_of_caller_supplied_client_id():
    request = Request({
        "type": "http", "method": "POST", "path": "/v1/claims",
        "headers": [(b"x-client-id", b"spoofed-id")],
        "client": ("203.0.113.17", 12345),
    })

    assert api._client_id(request) == "203.0.113.17"


def test_queue_propagates_trace_to_worker_and_graph(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from worker import main as worker

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    stored = []
    fake_redis = FakeRedis()
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
    monkeypatch.setattr(api, "r", fake_redis)
    monkeypatch.setattr(worker, "get_run", lambda _: stored[0])
    updates = []
    monkeypatch.setattr(worker, "update_run", lambda *args, **kwargs: updates.append(kwargs))
    response = client.post("/v1/claims", json=demo_claim(), headers={
        "traceparent": "00-12345678901234567890123456789012-1234567890123456-01",
        "x-correlation-id": "trace-test",
    })
    assert response.status_code == 202
    job = json.loads(fake_redis.lpush_calls[0][1])
    assert "traceparent" in job["trace_context"]
    worker.handle(job)
    spans = exporter.get_finished_spans()
    publish = next(span for span in spans if span.name == "queue.publish")
    consume = next(span for span in spans if span.name == "queue.process")
    assert consume.context.trace_id == publish.context.trace_id == int("12345678901234567890123456789012", 16)
    assert consume.parent.span_id == publish.context.span_id
    assert consume.attributes["run_id"] == response.json()["run_id"]
    assert consume.attributes["correlation_id"] == "trace-test"
    for name in ["agent.intake", "agent.research", "agent.verify", "agent.finalize", "tool.search_corpus"]:
        assert any(s.name == name and s.context.trace_id == consume.context.trace_id for s in spans)
    assert updates[-1]["status"] == "escalated"
    assert "pnr" not in str([dict(s.attributes) for s in spans])

    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(worker, "run_agent", fail)
    worker.handle({"run_id": job["run_id"], "cid": "legacy-job"})
    failed_span = [s for s in exporter.get_finished_spans() if s.name == "queue.process"][-1]
    assert failed_span.status.status_code == trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in failed_span.events)
    assert updates[-1]["result"]["escalation_reason"] == "PROVIDER_DEGRADED"

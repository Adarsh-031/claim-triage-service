from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main as api

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
        self.incr_calls = 0
        self.expire_calls = 0
        self.lpush_calls = []

    def incr(self, _key):
        self.incr_calls += 1
        return 1

    def expire(self, *_args):
        self.expire_calls += 1

    def lpush(self, *args):
        self.lpush_calls.append(args)


def demo_claim():
    scenario = json.loads(
        (ROOT / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text(encoding="utf-8")
    )
    return scenario["claim"]


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
    assert quarantine.client_id == "quarantine-test-client"
    assert len(quarantine.raw_body.encode("utf-8")) <= api.RAW_BODY_LIMIT
    assert fake_redis.incr_calls == 0
    assert fake_redis.lpush_calls == []


def test_quarantine_body_storage_is_capped(monkeypatch):
    stored = []
    monkeypatch.setattr(api, "SessionLocal", lambda: FakeSession(stored))
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
    assert fake_redis.incr_calls == 1
    assert len(fake_redis.lpush_calls) == 1
    assert fake_redis.lpush_calls[0][0] == api.settings.queue_name



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
    assert any(error["field"] == "destination" for error in payload["errors"])
    assert len(stored) == 1
    assert isinstance(stored[0], api.Quarantine)
    assert json.loads(stored[0].raw_body) == scenario["claim"]
    assert stored[0].client_id == "s02-acceptance-test"
    assert fake_redis.incr_calls == 0
    assert fake_redis.lpush_calls == []

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import redis
from triage_common.contracts import Route, RunStatus
from triage_common.store import recover_stale_run_state
from worker import main as worker_main


class FakeRedis:
    def __init__(self, queue=None, processing=None, lock_available=True):
        self.lists = {
            "triage:runs": list(queue or []),
            "triage:runs:processing": list(processing or []),
        }
        self.lock_available = lock_available
        self.lock = None

    def set(self, key, value, nx=False, ex=None):
        if not self.lock_available or self.lock is not None:
            return False
        self.lock = (key, value)
        return True

    def lrange(self, key, start, end):
        values = self.lists.setdefault(key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def blmove(self, source, destination, timeout, src="LEFT", dest="RIGHT"):
        values = self.lists.setdefault(source, [])
        if not values:
            return None
        value = values.pop() if src == "RIGHT" else values.pop(0)
        target = self.lists.setdefault(destination, [])
        target.insert(0, value) if dest == "LEFT" else target.append(value)
        return value

    def lrem(self, key, count, value):
        values = self.lists.setdefault(key, [])
        removed = 0
        while value in values and (count == 0 or removed < count):
            values.remove(value)
            removed += 1
        return removed

    def eval(self, script, numkeys, key, token):
        if self.lock == (key, token):
            self.lock = None
            return 1
        return 0


def _run(now, *, status="running", age_s=90, heartbeat_age_s=None, requeues=0):
    heartbeat_age_s = age_s if heartbeat_age_s is None else heartbeat_age_s
    return SimpleNamespace(
        status=status,
        heartbeat_at=now - timedelta(seconds=heartbeat_age_s),
        started_at=now - timedelta(seconds=age_s),
        updated_at=now - timedelta(seconds=heartbeat_age_s),
        created_at=now - timedelta(seconds=age_s),
        requeue_count=requeues,
        finished_at=None,
        error=None,
    )


def test_recovery_state_does_not_requeue_a_live_heartbeat():
    now = datetime.now(UTC)
    run = _run(now, age_s=90, heartbeat_age_s=5)
    assert recover_stale_run_state(run, now=now, stale_after_s=30) == "not_stale"
    assert run.status == RunStatus.running.value
    assert run.requeue_count == 0


def test_recovery_state_requeues_stale_run_and_clears_attempt_timestamps():
    now = datetime.now(UTC)
    run = _run(now, age_s=90, heartbeat_age_s=45, requeues=1)
    run.finished_at = now - timedelta(seconds=30)
    run.error = "old failure"
    assert recover_stale_run_state(run, now=now, stale_after_s=30, max_requeues=2) == "requeued"
    assert run.status == RunStatus.queued.value
    assert run.requeue_count == 1
    assert run.started_at is None
    assert run.heartbeat_at is None
    assert run.finished_at is None
    assert run.error is None


def test_recovery_state_fails_stale_run_at_retry_limit_without_requeueing():
    now = datetime.now(UTC)
    run = _run(now, age_s=90, heartbeat_age_s=45, requeues=3)
    assert recover_stale_run_state(run, now=now, stale_after_s=30, max_requeues=2) == "failed"
    assert run.status == RunStatus.failed.value
    assert run.requeue_count == 3
    assert run.finished_at == now
    assert run.heartbeat_at is None
    assert run.error == "Automated processing failed after recovery attempts; human review is required."
    assert run.result["route"] == Route.ESCALATE.value
    assert run.result["escalation_reason"] == "WORKER_FAILURE"


def test_terminal_or_future_handoff_state_is_not_requeued():
    now = datetime.now(UTC)
    for status, expected in [(RunStatus.done.value, "terminal"), ("verifying", "not_stale")]:
        run = _run(now, status=status, age_s=3600, heartbeat_age_s=3600)
        assert recover_stale_run_state(run, now=now, stale_after_s=30) == expected
        assert run.status == status
        assert run.requeue_count == 0


def test_reaper_requeues_stale_processing_entry(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4()), "cid": "test"})
    r = FakeRedis(processing=[raw])
    monkeypatch.setattr(worker_main, "recover_stale_run", lambda *args, **kwargs: "requeued")
    assert worker_main.reap_once(r) == {"requeued": 1, "failed": 0}
    assert r.lists["triage:runs"] == [raw]
    assert r.lists["triage:runs:processing"] == []
    assert r.lock is None


def test_reaper_marks_exhausted_run_failed_without_requeue(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(processing=[raw])
    monkeypatch.setattr(worker_main, "recover_stale_run", lambda *args, **kwargs: "failed")
    assert worker_main.reap_once(r) == {"requeued": 0, "failed": 1}
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == []


def test_terminal_processing_entry_is_acknowledged_without_requeue(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(processing=[raw])
    monkeypatch.setattr(worker_main, "recover_stale_run", lambda *args, **kwargs: "terminal")
    assert worker_main.reap_once(r) == {"requeued": 0, "failed": 0}
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == []


def test_only_one_worker_runs_the_reaper(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(processing=[raw], lock_available=False)
    monkeypatch.setattr(
        worker_main,
        "recover_stale_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reaper should not run")),
    )
    assert worker_main.reap_once(r) == {"requeued": 0, "failed": 0}
    assert r.lists["triage:runs:processing"] == [raw]


def _patch_worker_store(monkeypatch, run, updates):
    monkeypatch.setattr(worker_main, "get_run", lambda _run_id: run)
    def claim(_run_id, now=None):
        updates.append({"status": RunStatus.running.value, "started_at": now})
        return 1
    def finish(_run_id, attempt, **fields):
        updates.append(fields)
        return True
    monkeypatch.setattr(worker_main, "claim_run", claim)
    monkeypatch.setattr(worker_main, "finish_run", finish)
    monkeypatch.setattr(worker_main, "heartbeat_run", lambda *_args, **_kwargs: True)


def test_worker_records_execution_exception_without_exposing_provider_details(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    updates = []
    _patch_worker_store(monkeypatch, run, updates)
    monkeypatch.setattr(
        worker_main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("https://internal/provider timed out")),
    )
    assert worker_main._handle({"run_id": str(run_id)}) is True
    assert updates[0]["status"] == RunStatus.running.value
    assert updates[1]["status"] == RunStatus.failed.value
    assert updates[1]["error"] == worker_main.PUBLIC_WORKER_ERROR
    assert "https://internal/provider timed out" in str(updates[1]["trace"])
    assert updates[1]["result"]["route"] == Route.ESCALATE.value
    assert updates[1]["finished_at"] is not None


def test_worker_does_not_execute_when_compare_and_set_claim_is_lost(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    monkeypatch.setattr(worker_main, "get_run", lambda _run_id: run)
    monkeypatch.setattr(worker_main, "claim_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker_main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute duplicate")),
    )
    assert worker_main._handle({"run_id": str(run_id)}) is False


def test_completed_run_uses_done_status_and_lifecycle_timestamps(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    updates = []
    result = SimpleNamespace(route=Route.PAY, summary="Eligible", amount=100.0, citations=[], escalation_reason=None)
    _patch_worker_store(monkeypatch, run, updates)
    monkeypatch.setattr(worker_main, "run_agent", lambda *args, **kwargs: (result, [{"step": "finalize"}]))
    assert worker_main._handle({"run_id": str(run_id)}) is True
    assert updates[0]["status"] == RunStatus.running.value
    assert updates[0]["started_at"] is not None
    assert updates[1]["status"] == "done"
    assert updates[1]["finished_at"] is not None
    assert updates[1]["result"]["compensation_inr"] == 100.0


def test_consume_once_claims_then_acknowledges_persisted_result(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(queue=[raw])
    monkeypatch.setattr(worker_main, "handle", lambda _job: True)
    assert worker_main.consume_once(r) is True
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == []


def test_consume_once_keeps_processing_item_when_handler_raises(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(queue=[raw])
    monkeypatch.setattr(worker_main, "handle", lambda _job: (_ for _ in ()).throw(RuntimeError("temporary DB outage")))
    assert worker_main.consume_once(r) is False
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == [raw]


def test_consume_once_survives_redis_receive_errors(monkeypatch):
    class BrokenRedis(FakeRedis):
        def blmove(self, *args, **kwargs):
            raise redis.ConnectionError("temporary disconnect")
    monkeypatch.setattr(worker_main, "sleep", lambda _seconds: None)
    assert worker_main.consume_once(BrokenRedis()) is False


def test_worker_does_not_acknowledge_when_finalization_loses_ownership(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    updates = []
    result = SimpleNamespace(route=Route.PAY, summary="Eligible", amount=100.0, citations=[], escalation_reason=None)
    _patch_worker_store(monkeypatch, run, updates)
    monkeypatch.setattr(worker_main, "finish_run", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker_main, "run_agent", lambda *args, **kwargs: (result, [{"step": "finalize"}]))
    assert worker_main._handle({"run_id": str(run_id)}) is False


def test_worker_does_not_acknowledge_when_failure_result_loses_ownership(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    _patch_worker_store(monkeypatch, run, [])
    monkeypatch.setattr(worker_main, "finish_run", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        worker_main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failure")),
    )
    assert worker_main._handle({"run_id": str(run_id)}) is False


def test_consume_once_leaves_processing_entry_when_attempt_loses_ownership(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis(queue=[raw])
    monkeypatch.setattr(worker_main, "handle", lambda _job: False)
    assert worker_main.consume_once(r) is False
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == [raw]


def test_consume_once_keeps_entry_when_worker_claim_is_lost(monkeypatch):
    run_id = uuid4()
    raw = json.dumps({"run_id": str(run_id)})
    r = FakeRedis(queue=[raw])
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    monkeypatch.setattr(worker_main, "get_run", lambda _run_id: run)
    monkeypatch.setattr(worker_main, "claim_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker_main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute duplicate")),
    )
    assert worker_main.consume_once(r) is False
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == [raw]

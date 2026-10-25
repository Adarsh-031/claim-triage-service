import json
from types import SimpleNamespace
from uuid import uuid4

from triage_common.contracts import Route, RunStatus
from worker import main as worker_main


class FakeRedis:
    def __init__(self, processing=None, lock_available=True):
        self.lists = {"triage:runs": [], "triage:runs:processing": list(processing or [])}
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


def test_reaper_requeues_stale_processing_entry(monkeypatch):
    run_id = str(uuid4())
    raw = json.dumps({"run_id": run_id, "cid": "test"})
    r = FakeRedis([raw])
    monkeypatch.setattr(worker_main, "recover_stale_run", lambda *args, **kwargs: "requeued")

    result = worker_main.reap_once(r)

    assert result == {"requeued": 1, "failed": 0}
    assert r.lists["triage:runs"] == [raw]
    assert r.lists["triage:runs:processing"] == []
    assert r.lock is None


def test_reaper_marks_exhausted_run_failed_without_requeue(monkeypatch):
    run_id = str(uuid4())
    raw = json.dumps({"run_id": run_id})
    r = FakeRedis([raw])
    monkeypatch.setattr(worker_main, "recover_stale_run", lambda *args, **kwargs: "failed")

    result = worker_main.reap_once(r)

    assert result == {"requeued": 0, "failed": 1}
    assert r.lists["triage:runs"] == []
    assert r.lists["triage:runs:processing"] == []


def test_only_one_worker_runs_the_reaper(monkeypatch):
    raw = json.dumps({"run_id": str(uuid4())})
    r = FakeRedis([raw], lock_available=False)
    monkeypatch.setattr(
        worker_main,
        "recover_stale_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reaper should not run")),
    )

    assert worker_main.reap_once(r) == {"requeued": 0, "failed": 0}
    assert r.lists["triage:runs:processing"] == [raw]


def test_worker_records_execution_exception_as_failed(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    updates = []
    monkeypatch.setattr(worker_main, "get_run", lambda _run_id: run)
    monkeypatch.setattr(worker_main, "update_run", lambda _run_id, **fields: updates.append(fields))
    monkeypatch.setattr(
        worker_main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider timed out")),
    )

    assert worker_main._handle({"run_id": str(run_id)}) is True
    assert updates[0]["status"] == RunStatus.running.value
    assert updates[1]["status"] == RunStatus.failed.value
    assert updates[1]["error"] == "provider timed out"
    assert updates[1]["finished_at"] is not None


def test_completed_run_uses_done_status_and_lifecycle_timestamps(monkeypatch):
    run_id = uuid4()
    run = SimpleNamespace(status=RunStatus.queued.value, claim={"claim": "example"})
    updates = []
    result = SimpleNamespace(route=Route.PAY, summary="Eligible", amount=100.0, citations=[], escalation_reason=None)
    monkeypatch.setattr(worker_main, "get_run", lambda _run_id: run)
    monkeypatch.setattr(worker_main, "update_run", lambda _run_id, **fields: updates.append(fields))
    monkeypatch.setattr(worker_main, "run_agent", lambda *args, **kwargs: (result, [{"step": "finalize"}]))

    assert worker_main._handle({"run_id": str(run_id)}) is True
    assert updates[0]["status"] == RunStatus.running.value
    assert updates[0]["started_at"] is not None
    assert updates[1]["status"] == "done"
    assert updates[1]["finished_at"] is not None
    assert updates[1]["result"]["compensation_inr"] == 100.0

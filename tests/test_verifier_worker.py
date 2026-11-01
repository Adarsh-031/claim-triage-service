"""verifier-worker hand-off (#28): agent-worker -> drafts row + verify queue -> verifier-worker.

Runs the real worker and verifier handlers against an in-memory SQLite copy of the store
schema and a fake Redis, so the whole hand-off is exercised without containers.
"""
import json
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from triage_common import store
from triage_common.contracts import EscalationReason, Route
from worker import main as worker_main
from worker import verifier_main
from worker.agent import graph

from tests.test_worker_reliability import FakeRedis

S01 = json.loads(Path("eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text())

pytestmark = pytest.mark.usefixtures("research_env")


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    store.Base.metadata.create_all(engine)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    return store


@pytest.fixture
def queues(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(worker_main, "cache_redis", r)
    monkeypatch.setattr(verifier_main, "r_client", r)
    monkeypatch.setattr(worker_main.settings, "verifier_mode", "split")
    monkeypatch.setattr(worker_main.settings, "cache_enabled", False)
    # Undo the shared no-DB stub: these tests check the stored rows.
    monkeypatch.setattr(worker_main, "record_verifier_outcomes", store.record_verifier_outcomes)
    return r


def host(monkeypatch, name):
    monkeypatch.setattr(socket, "gethostname", lambda: name)


def new_run(db, claim=None):
    run_id = uuid4()
    with db.SessionLocal() as session, session.begin():
        session.add(db.Run(id=run_id, status="queued", claim=claim or S01["claim"]))
    return run_id


def pop(r, queue):
    return json.loads(r.lists[queue].pop())


def run_row(db, run_id):
    return db.get_run(run_id)


def agent(monkeypatch, r, job):
    host(monkeypatch, "agent-worker-1")
    assert worker_main.handle(job) is True


def verifier(monkeypatch, r, name="verifier-worker-1"):
    host(monkeypatch, name)
    return verifier_main.handle(pop(r, verifier_main.settings.verify_queue_name))


def test_split_run_is_verified_in_another_process_and_trace_shows_both_hosts(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id), "cid": "split"})
    assert run_row(db, run_id).status == "verifying"
    draft = db.get_draft(run_id, 1)
    assert draft.status == "pending" and draft.agent_host == "agent-worker-1"
    assert "messages" not in draft.state and "deadline_epoch" in draft.state

    assert verifier(monkeypatch, queues) is True
    run = run_row(db, run_id)
    assert run.status == "done"
    assert run.result["route"] == "decline" and run.result["verified"] is True
    hosts = {t["host"] for t in run.trace if t.get("host")}
    assert hosts == {"agent-worker-1", "verifier-worker-1"}
    assert [t["step"] for t in run.trace][-3:] == ["handoff", "verify", "finalize"]
    assert db.get_draft(run_id, 1).status == "finalized"
    with db.SessionLocal() as session:
        rows = session.query(db.VerifierOutcome).filter_by(run_id=run_id).all()
    assert len(rows) == 1 and rows[0].verified is True


def test_verifier_retry_goes_back_through_the_agent_worker_then_escalates(db, queues, monkeypatch):
    original = graph.make_draft

    def fabricate(state):
        draft = original(state)
        draft["citations"] = [{**draft["citations"][0], "quote": "Invented rule."}, *draft["citations"][1:]]
        return draft

    monkeypatch.setattr(graph, "make_draft", fabricate)
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    for expected_attempt in (1, 2):
        assert verifier(monkeypatch, queues) is True
        run = run_row(db, run_id)
        assert run.status == "queued" and run.resume_count == expected_attempt
        job = pop(queues, worker_main.settings.queue_name)
        assert job["resume"] is True
        agent(monkeypatch, queues, job)
        assert db.get_draft(run_id, expected_attempt + 1).verify_attempt == expected_attempt
    assert verifier(monkeypatch, queues) is True
    run = run_row(db, run_id)
    assert run.status == "escalated" and run.result["escalation_reason"] == "VERIFICATION_FAILED"
    assert "Invented rule." not in json.dumps(run.result)
    assert [t.get("strategy") for t in run.trace if t["step"] == "verify_retry"] == ["cross_references", "bm25"]
    # Verifier resumes are planned work: the crash-recovery limit is untouched.
    assert run.requeue_count == 3 and run.resume_count == 2
    assert store.recover_stale_run_state(run, now=datetime.now(UTC)) == "terminal"


def test_killed_verifier_is_reclaimed_by_the_reaper_and_the_run_completes(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    # A verifier takes the message into its processing list, claims the draft, then dies.
    raw = queues.blmove(verifier_main.settings.verify_queue_name, verifier_main._processing_key(), 5)
    assert db.claim_draft(run_id, 1, host="verifier-dead") == 1
    monkeypatch.setattr(verifier_main, "HEARTBEAT_STALE_AFTER_S", -1)
    assert verifier_main.reap_once(queues) == {"requeued": 1, "failed": 0}
    assert db.get_draft(run_id, 1).status == "pending"
    assert queues.lists[verifier_main._processing_key()] == []
    assert json.loads(queues.lists[verifier_main.settings.verify_queue_name][0]) == json.loads(raw)

    assert verifier(monkeypatch, queues, "verifier-worker-2") is True
    run = run_row(db, run_id)
    assert run.status == "done" and run.result["verified"] is True
    assert db.get_draft(run_id, 1).verifier_host == "verifier-worker-2"


def test_repeatedly_killed_verifier_escalates_instead_of_losing_the_run(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    monkeypatch.setattr(verifier_main, "HEARTBEAT_STALE_AFTER_S", -1)
    outcomes = []
    for _ in range(verifier_main.MAX_RECLAIMS + 1):
        queues.blmove(verifier_main.settings.verify_queue_name, verifier_main._processing_key(), 5)
        assert db.claim_draft(run_id, 1, host="verifier-dead") is not None
        outcomes.append(verifier_main.reap_once(queues))
    assert outcomes[-1] == {"requeued": 0, "failed": 1}
    run = run_row(db, run_id)
    assert run.status == "escalated" and run.result["escalation_reason"] == "VERIFICATION_FAILED"
    assert queues.lists.get(verifier_main.settings.verify_queue_name, []) == []


def test_stale_verifier_cannot_publish_after_reclaim(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    first = db.claim_draft(run_id, 1, host="slow")
    db.recover_stale_draft(run_id, 1, stale_after_s=-1)
    second = db.claim_draft(run_id, 1, host="fresh")
    assert second == first + 1
    assert db.finish_verified_run(run_id, 1, first, status="done", result={}) is False
    assert db.heartbeat_draft(run_id, 1, first) is False
    assert run_row(db, run_id).status == "verifying"


def test_draft_nobody_verifies_escalates_after_the_wait(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    assert db.expire_waiting_drafts(wait_s=120) == []
    later = datetime.now(UTC) + timedelta(seconds=121)
    assert db.expire_waiting_drafts(wait_s=120, now=later) == [run_id]
    run = run_row(db, run_id)
    assert run.status == "escalated" and run.result["escalation_reason"] == EscalationReason.VERIFICATION_FAILED
    # A late verifier finds nothing to claim and acknowledges the message.
    assert verifier(monkeypatch, queues) is True
    assert run_row(db, run_id).status == "escalated"


def test_early_escalation_finishes_in_the_agent_worker_without_a_handoff(db, queues, monkeypatch):
    claim = json.loads(Path("eval/scenarios/s22_vietnam_route_out_of_corpus.json").read_text())["claim"]
    run_id = new_run(db, claim)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    run = run_row(db, run_id)
    assert run.status == "escalated" and run.result["escalation_reason"] == "OUT_OF_CORPUS"
    assert db.get_draft(run_id, 1) is None
    assert queues.lists.get(verifier_main.settings.verify_queue_name, []) == []


def test_state_round_trip_keeps_the_remaining_wall_clock_budget():
    state = graph.run_until_handoff(S01["claim"])
    assert state["awaiting_verification"] is True
    data = graph.dump_state(state)
    json.dumps(data)
    loaded = graph.load_state(data)
    assert loaded["draft"]["route"] is Route.DECLINE
    assert abs(loaded["started_at"] - state["started_at"]) < 1
    verified, decision = graph.verify_once(loaded)
    assert decision == "finalize" and verified["result"]["route"] == "decline"


def test_agent_worker_reaper_recovers_a_draft_when_no_verifier_survives(db, queues, monkeypatch):
    run_id = new_run(db)
    agent(monkeypatch, queues, {"run_id": str(run_id)})
    queues.blmove(verifier_main.settings.verify_queue_name, verifier_main._processing_key(), 5)
    db.claim_draft(run_id, 1, host="verifier-dead")
    monkeypatch.setattr(verifier_main, "HEARTBEAT_STALE_AFTER_S", -1)
    worker_main.reap_once(queues)  # reclaims the draft back to pending
    assert db.get_draft(run_id, 1).status == "pending"
    monkeypatch.setattr(worker_main.settings, "verify_wait_s", 0.001)
    time.sleep(0.01)
    worker_main.reap_once(queues)  # nobody verified it in time
    assert run_row(db, run_id).status == "escalated"


def test_accepted_alternate_decline_lists_meals_and_does_not_ask_for_the_cause():
    result, _ = graph.run_agent(S01["claim"])
    assert result.route is Route.DECLINE
    assert result.entitlements == ["Meals and refreshments in relation to waiting time (CAR 3.8.1(a))"]
    assert "stated_cause" not in result.summary

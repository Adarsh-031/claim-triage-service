import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from triage_common.contracts import EscalationReason, Recommendation, Result, Route, RunStatus
from worker import main as worker_main
from worker.agent import graph
from worker.tools.corpus import DEMO_CHUNKS, DEMO_CORPUS_VERSION, search_corpus

SCENARIO = json.loads(
    (Path(__file__).parents[1] / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text()
)


def test_s01_returns_valid_cited_result_and_ordered_trace():
    run_id = str(uuid4())
    result, trace = graph.run_agent(SCENARIO["claim"], "test-cid", run_id=run_id)

    assert Result.model_validate_json(result.model_dump_json()) == result
    assert str(result.run_id) == run_id
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert {citation.clause_id for citation in result.citations} == set(SCENARIO["expected"]["must_cite"])
    chunks = {chunk.chunk_id: chunk for chunk in DEMO_CHUNKS}
    for citation in result.citations:
        chunk = chunks[citation.chunk_id]
        assert citation.doc_id == chunk.doc_id
        assert citation.quote in chunk.text
    assert "No claim determination made" in result.summary
    assert result.corpus_version == DEMO_CORPUS_VERSION
    assert [entry["step"] for entry in trace] == ["start", "intake", "research", "verify", "finalize"]
    assert trace[0]["cid"] == "test-cid"
    assert trace[-1]["step_count"] == 3
    assert trace[-1]["tool_calls"] == 1
    assert trace[-2]["verified"] is False


@pytest.mark.parametrize("budget", ["steps", "tools"])
def test_looping_fake_node_finalizes_at_exact_budget(monkeypatch, budget):
    calls = []

    def looping_research(state):
        calls.append(1)
        state["tool_calls"] += budget == "tools"
        return state  # no draft, so the research edge loops

    monkeypatch.setattr(graph.settings, "agent_max_steps", 12 if budget == "steps" else 100)
    monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 15)
    monkeypatch.setattr(graph, "research", looping_research)
    monkeypatch.setattr(graph, "GRAPH", graph.build_graph())
    result, trace = graph.run_agent(SCENARIO["claim"])

    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert trace[-1]["step"] == "finalize"
    assert len(calls) == (10 if budget == "steps" else 15)
    assert trace[-1]["step_count" if budget == "steps" else "tool_calls"] == (12 if budget == "steps" else 15)


def test_search_reaching_time_limit_skips_drafting_and_escalates(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(graph.time, "monotonic", lambda: now[0])

    def slow_search(query):
        now[0] += 60.0
        return search_corpus(query)

    def unexpected_draft(state):
        pytest.fail("drafting must not start after search exhausts the time budget")

    monkeypatch.setattr(graph, "search_corpus", slow_search)
    monkeypatch.setattr(graph, "_stub_draft", unexpected_draft)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert [entry["step"] for entry in trace] == ["start", "intake", "research", "finalize"]


def test_tool_limit_prevents_search_and_returns_budget_escalation(monkeypatch):
    search = Mock()
    monkeypatch.setattr(graph, "search_corpus", search)
    monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert trace[-1]["tool_calls"] == 1
    search.assert_not_called()


def test_expired_run_skips_all_work_nodes(monkeypatch):
    monkeypatch.setattr(graph.settings, "agent_run_timeout_s", 0)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert [entry["step"] for entry in trace] == ["start", "finalize"]
    assert trace[-1]["step_count"] == trace[-1]["tool_calls"] == 0


def test_unsupported_claim_escalates_instead_of_making_up_a_decision():
    result, _ = graph.run_agent({**SCENARIO["claim"], "disruption_type": "delay"})
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS
    assert result.citations == []


def test_fixture_search_filters_and_limits():
    assert len(search_corpus("cancellation", k=2)) == 2
    assert search_corpus("cancellation", k=0) == []
    assert search_corpus("cancellation", doc_id="real-source") == []


@pytest.mark.parametrize("budget_exhausted", [False, True])
def test_worker_preserves_run_id_and_legacy_api_payload(monkeypatch, budget_exhausted):
    run_id = uuid4()
    updates = []
    monkeypatch.setattr(
        worker_main,
        "get_run",
        lambda _: SimpleNamespace(claim=SCENARIO["claim"], status=RunStatus.queued.value),
    )
    monkeypatch.setattr(worker_main, "claim_run",
        lambda rid, now=None: (updates.append((rid, {"status": RunStatus.running.value, "started_at": now})) or 0))
    monkeypatch.setattr(worker_main, "finish_run",
        lambda rid, attempt, **update: (updates.append((rid, update)) or True))
    monkeypatch.setattr(worker_main, "heartbeat_run", lambda *_args, **_kwargs: True)
    original_run_agent = graph.run_agent

    def checked_run_agent(claim, correlation_id, *, run_id):
        result, trace = original_run_agent(claim, correlation_id, run_id=run_id)
        assert str(result.run_id) == run_id
        return result, trace

    monkeypatch.setattr(worker_main, "run_agent", checked_run_agent)
    if budget_exhausted:
        monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    worker_main.handle({"run_id": str(run_id), "cid": "worker-test"})

    assert updates[0][0] == run_id
    assert updates[0][1]["status"] == RunStatus.running.value
    assert updates[0][1]["started_at"] is not None
    final = updates[-1][1]
    rec = Recommendation.model_validate(final["result"])
    assert final["status"] == "escalated"
    assert rec.requires_human_approval is True
    assert rec.verified is False
    if budget_exhausted:
        assert rec.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    else:
        assert rec.route is Route.ESCALATE
        assert rec.escalation_reason is EscalationReason.VERIFICATION_FAILED
        assert rec.citations


@pytest.mark.parametrize(("used", "invoked", "reserved"), [(13, True, 14), (14, False, 15), (15, False, 15)])
def test_research_reserves_budget_before_tool_side_effects(monkeypatch, used, invoked, reserved):
    search = Mock(return_value=search_corpus("cancellation"))
    monkeypatch.setattr(graph, "search_corpus", search)
    state = {
        "claim": SCENARIO["claim"], "facts": {"disruption_type": "cancellation"},
        "started_at": graph.time.monotonic(), "step_count": 2, "tool_calls": used,
    }
    graph.research(state)
    assert search.call_count == int(invoked)
    assert state["tool_calls"] == reserved
    assert state.get("budget_exceeded", False) is (not invoked)


def test_terminal_step_reservation_skips_node_side_effects():
    node = Mock()
    state = {"started_at": graph.time.monotonic(), "step_count": 11, "tool_calls": 0}
    out = graph._bounded(node)(state)
    node.assert_not_called()
    assert out["step_count"] == 12
    assert out["budget_exceeded"] is True


@pytest.mark.parametrize("route", [Route.DECLINE, Route.PAY, Route.REQUEST_EVIDENCE])
def test_worker_never_publishes_unverified_consequential_draft(monkeypatch, route):
    original_stub = graph._stub_draft

    def consequential_draft(state):
        return {**original_stub(state), "route": route, "summary": "Unverified decision",
                "escalation_reason": None, "amount": 1000}

    monkeypatch.setattr(graph, "_stub_draft", consequential_draft)
    updates = []
    run_id = uuid4()
    monkeypatch.setattr(
        worker_main,
        "get_run",
        lambda _: SimpleNamespace(claim=SCENARIO["claim"], status=RunStatus.queued.value),
    )
    monkeypatch.setattr(worker_main, "claim_run",
        lambda rid, now=None: (updates.append({"status": RunStatus.running.value, "started_at": now}) or 0))
    monkeypatch.setattr(worker_main, "finish_run",
        lambda rid, attempt, **update: (updates.append(update) or True))
    monkeypatch.setattr(worker_main, "heartbeat_run", lambda *_args, **_kwargs: True)
    worker_main.handle({"run_id": str(run_id)})
    saved = updates[-1]
    rec = Recommendation.model_validate(saved["result"])
    assert saved["status"] == "escalated"
    assert rec.route is Route.ESCALATE
    assert rec.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert rec.compensation_inr is None
    assert rec.verified is False
    assert rec.summary != "Unverified decision"
    assert rec.citations

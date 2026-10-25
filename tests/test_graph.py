import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from triage_common.contracts import EscalationReason, Recommendation, Result, Route, RunStatus
from worker import main as worker_main
from worker.agent import graph
from worker.tools.corpus import CORPUS_VERSION

SCENARIO = json.loads(
    (Path(__file__).parents[1] / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text()
)


def _sample_chunks():
    texts = {"3.3.1": "Cancellation may offer an alternate flight or refund.",
             "3.3.2": "Cancellation compensation and a full refund may apply.",
             "3.8.1(a)": "Meals and refreshments in relation to waiting time."}
    return [{"chunk_id": f"CAR-S3-M-IV-R4:{cid}:0001", "doc_id": "CAR-S3-M-IV-R4",
             "clause_id": cid, "heading_path": ["3 Cancellation"], "text": passage,
             "hash": hashlib.sha256(passage.encode()).hexdigest(), "page": None, "refs": []}
            for cid, passage in texts.items()]


def _fake_search_result():
    return {"chunks": _sample_chunks(), "retrieval_error": None}


def test_s01_returns_valid_cited_result_and_ordered_trace(monkeypatch):
    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=lambda _args: _fake_search_result()))
    run_id = str(uuid4())
    result, trace = graph.run_agent(SCENARIO["claim"], "test-cid", run_id=run_id)

    assert Result.model_validate_json(result.model_dump_json()) == result
    assert str(result.run_id) == run_id
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert {citation.clause_id for citation in result.citations} == set(SCENARIO["expected"]["must_cite"])
    chunks = {chunk["chunk_id"]: chunk for chunk in _sample_chunks()}
    for citation in result.citations:
        chunk = chunks[citation.chunk_id]
        assert citation.doc_id == chunk["doc_id"]
        assert citation.quote in chunk["text"]
    assert "No claim determination made" in result.summary
    assert result.corpus_version == CORPUS_VERSION
    assert [entry["step"] for entry in trace] == ["start", "intake", "research", "verify", "finalize"]
    assert trace[0]["cid"] == "test-cid"
    assert trace[-1]["step_count"] == 3
    assert trace[-1]["tool_calls"] == 2
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

    def slow_search(_args):
        now[0] += 60.0
        return _fake_search_result()

    def unexpected_draft(state):
        pytest.fail("drafting must not start after search exhausts the time budget")

    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=slow_search))
    monkeypatch.setattr(graph, "_stub_draft", unexpected_draft)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert [entry["step"] for entry in trace] == ["start", "intake", "research", "finalize"]


def test_tool_limit_prevents_search_and_returns_budget_escalation(monkeypatch):
    search = Mock()
    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=search))
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


def test_unsupported_claim_escalates_instead_of_making_up_a_decision(monkeypatch):
    monkeypatch.setattr(
        graph, "search_corpus",
        SimpleNamespace(invoke=lambda _args: {"chunks": [], "retrieval_error": None}),
    )
    result, _ = graph.run_agent({**SCENARIO["claim"], "disruption_type": "delay"})
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS
    assert result.citations == []


@pytest.mark.parametrize("budget_exhausted", [False, True])
def test_worker_preserves_run_id_and_legacy_api_payload(monkeypatch, budget_exhausted):
    run_id = uuid4()
    updates = []
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))
    monkeypatch.setattr(worker_main, "update_run", lambda rid, **update: updates.append((rid, update)))
    original_run_agent = graph.run_agent

    def checked_run_agent(claim, correlation_id, *, run_id):
        result, trace = original_run_agent(claim, correlation_id, run_id=run_id)
        assert str(result.run_id) == run_id
        return result, trace

    monkeypatch.setattr(worker_main, "run_agent", checked_run_agent)
    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=lambda _args: _fake_search_result()))
    if budget_exhausted:
        monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    worker_main.handle({"run_id": str(run_id), "cid": "worker-test"})

    assert updates[0] == (run_id, {"status": RunStatus.running.value})
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


@pytest.mark.parametrize(("used", "invoked", "reserved"), [(12, 2, 14), (13, 1, 15), (14, 0, 15), (15, 0, 15)])
def test_research_reserves_budget_before_tool_side_effects(monkeypatch, used, invoked, reserved):
    search = Mock(return_value=_fake_search_result())
    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=search))
    state = {
        "claim": SCENARIO["claim"], "facts": {"disruption_type": "cancellation"},
        "started_at": graph.time.monotonic(), "step_count": 2, "tool_calls": used,
    }
    graph.research(state)
    assert search.call_count == invoked
    assert state["tool_calls"] == reserved
    assert state.get("budget_exceeded", False) is (invoked < 2)


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
    monkeypatch.setattr(graph, "search_corpus", SimpleNamespace(invoke=lambda _args: _fake_search_result()))
    updates = []
    run_id = uuid4()
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))
    monkeypatch.setattr(worker_main, "update_run", lambda rid, **update: updates.append(update))
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

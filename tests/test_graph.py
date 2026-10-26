import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from triage_common.contracts import EscalationReason, Recommendation, Result, Route, RunStatus
from worker import main as worker_main
from worker.agent import graph
from worker.tools import corpus

SCENARIO_DIR = Path(__file__).parents[1] / "eval/scenarios"
SCENARIO = json.loads((SCENARIO_DIR / "s01_demo_cancellation_alternate_taken.json").read_text())

pytestmark = pytest.mark.usefixtures("research_env")


def tool_call(name, **arguments):
    return {"id": "test-tool", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_s01_research_preserves_real_citations_and_requires_comparison_review():
    run_id = str(uuid4())
    result, trace = graph.run_agent(SCENARIO["claim"], "test-cid", run_id=run_id)
    assert Result.model_validate_json(result.model_dump_json()) == result
    assert str(result.run_id) == run_id
    # Research is complete; the separate grounding verifier remains a required gate.
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["route"] == Route.REQUEST_EVIDENCE
    assert draft["amount"] is None and draft["compensation"]["amount"] == 0
    assert draft["checklist"]["airline_comparison"]["status"] == "missing"
    assert "unverified" in draft["checklist"]["airline_comparison"]["reason"]
    assert "compensation declined" not in draft["summary"]
    assert "Meals and refreshments are owed" not in draft["summary"]
    assert set(SCENARIO["expected"]["must_cite"]) <= {c.clause_id for c in result.citations}
    chunks = corpus.get_corpus().chunks
    for citation in result.citations:
        chunk = chunks[citation.chunk_id]
        assert citation.doc_id == chunk["doc_id"]
        assert citation.quote in chunk["text"]
        assert not citation.chunk_id.startswith("DEMO")
    assert result.prompt_version == "research-v1"
    assert result.corpus_version == corpus.get_corpus().version
    assert trace[0]["cid"] == "test-cid"
    assert trace[-1]["step_count"] == 8
    assert trace[-1]["tool_calls"] == 14
    assert trace[-2]["verified"] is False
    assert {t["name"] for t in trace if t["step"] == "tool"} == {
        "search_corpus", "get_clause", "get_references", "get_referrers", "get_definition",
        "get_claim_field", "compute_compensation",
    }
    assert all(item["status"] in {"cited", "missing"} for item in draft["checklist"].values())
    assert draft["checklist"]["extraordinary_circumstances"]["missing_fields"] == ["stated_cause"]


def test_technical_fault_escalates_conflict_with_car_and_airline_citations():
    scenario = json.loads((SCENARIO_DIR / "s21_cancel_technical_fault_evidence.json").read_text())
    result, _ = graph.run_agent(scenario["claim"])
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.PROVISION_CONFLICT
    assert result.amount is None
    assert {c.doc_id for c in result.citations} == {corpus.CAR_DOC, "INDIGO-COC-DOM"}
    assert {c.clause_id for c in result.citations} == {"1.4", "1.1"}
    assert all(c.quote in corpus.get_corpus().chunks[c.chunk_id]["text"] for c in result.citations)
    assert "beyond the airline's control" in result.summary


@pytest.mark.parametrize("prompt_version", ["v1", "v2"])
def test_run_agent_preserves_requested_prompt_version(prompt_version):
    result, _ = graph.run_agent(SCENARIO["claim"], prompt_version=prompt_version)
    assert result.prompt_version == prompt_version


def test_run_agent_rejects_unknown_prompt_version():
    with pytest.raises(ValueError, match="prompt_version"):
        graph.run_agent(SCENARIO["claim"], prompt_version="v3")


@pytest.mark.parametrize("budget", ["steps", "tools"])
def test_looping_fake_node_finalizes_at_exact_budget(monkeypatch, budget):
    calls = []

    def looping_research(state):
        calls.append(1)
        state["tool_calls"] += budget == "tools"
        return state

    monkeypatch.setattr(graph.settings, "agent_max_steps", 12 if budget == "steps" else 100)
    monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 15)
    monkeypatch.setattr(graph, "research", looping_research)
    monkeypatch.setattr(graph, "GRAPH", graph.build_graph())
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert len(calls) == (10 if budget == "steps" else 15)
    assert trace[-1]["step_count" if budget == "steps" else "tool_calls"] == (12 if budget == "steps" else 15)


@pytest.mark.parametrize("budget", ["steps", "tools"])
def test_real_tool_loop_exhausts_exact_budget(monkeypatch, budget):
    def looping_reply(messages, tools, **kwargs):
        calls = [tool_call("get_clause", clause_id="3.3.2")] * (1 if budget == "steps" else 20)
        return {"role": "assistant", "content": None, "tool_calls": calls}

    monkeypatch.setattr(graph, "get_llm", lambda: SimpleNamespace(chat=looping_reply))
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert trace[-1]["step_count" if budget == "steps" else "tool_calls"] == (12 if budget == "steps" else 15)
    assert sum(t["step"] == "tool" for t in trace) == (10 if budget == "steps" else 14)
    assert not any(t["step"] == "draft" for t in trace)


def test_tool_reaching_time_limit_skips_rest_of_batch_and_draft(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(graph.time, "monotonic", lambda: now[0])
    original = graph.execute_tool
    calls = []

    def slow_tool(name, arguments, claim):
        calls.append(name)
        output = original(name, arguments, claim)
        now[0] += 60
        return output

    monkeypatch.setattr(graph, "execute_tool", slow_tool)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert calls == ["search_corpus"]
    assert not any(t["step"] == "draft" for t in trace)


def test_provider_reaching_time_limit_skips_tools_and_draft(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(graph.time, "monotonic", lambda: now[0])

    def slow_provider(messages, tools, *, timeout_s):
        assert timeout_s == 60
        now[0] += 60
        return {"tool_calls": [tool_call("get_clause", clause_id="3.3.2")]}

    monkeypatch.setattr(graph, "get_llm", lambda: SimpleNamespace(chat=slow_provider))
    execute = Mock()
    monkeypatch.setattr(graph, "execute_tool", execute)
    result, _ = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    execute.assert_not_called()


def test_tool_limit_prevents_tool_side_effects(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(graph, "execute_tool", execute)
    monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert trace[-1]["tool_calls"] == 1
    execute.assert_not_called()


def test_expired_run_skips_all_work_nodes(monkeypatch):
    monkeypatch.setattr(graph.settings, "agent_run_timeout_s", 0)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert [t["step"] for t in trace] == ["start", "finalize"]
    assert trace[-1]["step_count"] == trace[-1]["tool_calls"] == 0


def test_unsupported_claim_escalates_instead_of_making_up_a_decision():
    result, _ = graph.run_agent({**SCENARIO["claim"], "disruption_type": "unknown"})
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS


@pytest.mark.parametrize("used,invoked,reserved", [(13, True, 14), (14, False, 15), (15, False, 15)])
def test_research_reserves_before_each_tool(monkeypatch, used, invoked, reserved):
    provider = SimpleNamespace(chat=lambda *a, **kw: {
        "tool_calls": [tool_call("get_clause", clause_id="3.3.2")], "content": None,
    })
    monkeypatch.setattr(graph, "get_llm", lambda: provider)
    state = graph.intake({"claim": SCENARIO["claim"], "started_at": graph.time.monotonic(),
                          "step_count": 2, "tool_calls": used})
    execute = Mock(return_value=None)
    monkeypatch.setattr(graph, "execute_tool", execute)
    graph.research(state)
    assert execute.call_count == int(invoked)
    assert state["tool_calls"] == reserved
    assert state.get("budget_exceeded", False) is (not invoked)


def test_terminal_step_reservation_skips_node_side_effects():
    node = Mock()
    state = {"started_at": graph.time.monotonic(), "step_count": 11, "tool_calls": 0}
    out = graph._bounded(node)(state)
    node.assert_not_called()
    assert out["step_count"] == 12 and out["budget_exceeded"] is True


@pytest.mark.parametrize("budget_exhausted", [False, True])
def test_worker_preserves_run_id_and_legacy_api_payload(monkeypatch, budget_exhausted):
    run_id = uuid4()
    updates = []
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))
    monkeypatch.setattr(worker_main, "update_run", lambda rid, **update: updates.append((rid, update)))
    original = graph.run_agent

    def checked_run_agent(claim, correlation_id, *, run_id, prompt_version="v1"):
        result, trace = original_run_agent(claim, correlation_id, run_id=run_id, prompt_version=prompt_version)
        assert str(result.run_id) == run_id
        return result, trace

    monkeypatch.setattr(worker_main, "run_agent", checked)
    if budget_exhausted:
        monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    worker_main.handle({"run_id": str(run_id), "cid": "worker-test"})
    assert updates[0] == (run_id, {"status": RunStatus.running.value})
    final = updates[-1][1]
    rec = Recommendation.model_validate(final["result"])
    assert final["status"] == "escalated"
    assert rec.requires_human_approval is True and rec.verified is False
    assert rec.escalation_reason is (EscalationReason.BUDGET_EXCEEDED if budget_exhausted
                                     else EscalationReason.VERIFICATION_FAILED)


@pytest.mark.parametrize("route", [Route.DECLINE, Route.PAY, Route.REQUEST_EVIDENCE])
def test_worker_never_publishes_unverified_consequential_draft(monkeypatch, route):
    original = graph.make_draft

    def consequential(state):
        return {**original(state), "route": route, "summary": "Unverified decision", "amount": 1000}

    monkeypatch.setattr(graph, "make_draft", consequential)
    updates = []
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))
    monkeypatch.setattr(worker_main, "update_run", lambda rid, **update: updates.append(update))
    worker_main.handle({"run_id": str(uuid4())})
    saved = updates[-1]
    rec = Recommendation.model_validate(saved["result"])
    assert saved["status"] == "escalated"
    assert rec.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert rec.compensation_inr is None and rec.verified is False
    assert rec.summary != "Unverified decision" and rec.citations


def test_missing_corpus_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(graph.settings, "corpus_dir", str(tmp_path))
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS
    assert result.corpus_version == "unavailable"
    assert not any(t["step"] == "tool" for t in trace)


def test_provider_failure_returns_valid_escalation(monkeypatch):
    provider = SimpleNamespace(chat=Mock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(graph, "get_llm", lambda: provider)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.PROVIDER_DEGRADED
    assert trace[-1]["step"] == "finalize"


def test_invalid_tool_arguments_are_returned_to_provider_and_bounded(monkeypatch):
    provider = SimpleNamespace(chat=lambda *a, **kw: {
        "tool_calls": [tool_call("compute_compensation", clause_id="3.3.2", inputs={"amount": 999999})],
    })
    monkeypatch.setattr(graph, "get_llm", lambda: provider)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert all(t["error"] for t in trace if t["step"] == "tool")
    assert not any(t["step"] == "draft" for t in trace)


def test_model_cannot_inject_amount_or_skip_source_coverage(monkeypatch):
    provider = SimpleNamespace(chat=lambda *a, **kw: {
        "content": json.dumps({"comparison": {"status": "missing"}, "amount": 999999,
                               "summary": "Pay now", "checklist": "complete"}),
    })
    monkeypatch.setattr(graph, "get_llm", lambda: provider)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.amount is None and "Pay now" not in result.summary
    draft = next(t for t in trace if t["step"] == "draft")
    assert all(item["status"] == "missing" for item in draft["checklist"].values())
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS


def test_missing_facilities_chunk_cannot_support_a_meals_draft(monkeypatch):
    original = graph.execute_tool

    def no_meals(name, arguments, claim):
        if name == "get_clause" and arguments.get("clause_id") == "3.8.1(a)":
            return None
        return original(name, arguments, claim)

    monkeypatch.setattr(graph, "execute_tool", no_meals)
    _, trace = graph.run_agent(SCENARIO["claim"])
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["route"] == Route.REQUEST_EVIDENCE
    assert "Meals and refreshments are owed" not in draft["summary"]
    assert draft["checklist"]["meals_facilities"]["status"] == "missing"
    assert f"{corpus.CAR_DOC}:3.8.1(a)" in draft["checklist"]["meals_facilities"]["missing_chunks"]


@pytest.mark.parametrize("reply", [None, {"tool_calls": [None]}, {"tool_calls": "invalid"}])
def test_malformed_provider_envelope_escalates(monkeypatch, reply):
    monkeypatch.setattr(graph, "get_llm", lambda: SimpleNamespace(chat=lambda *a, **kw: reply))
    result, _ = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.PROVIDER_DEGRADED


def test_fabricated_comparison_citations_cannot_finish_research(monkeypatch):
    provider = SimpleNamespace(chat=lambda *a, **kw: {"content": json.dumps({"comparison": {
        "status": "conflict", "car_chunk_id": "fabricated-car", "indigo_chunk_id": "fabricated-coc",
    }})})
    monkeypatch.setattr(graph, "get_llm", lambda: provider)
    result, trace = graph.run_agent(SCENARIO["claim"])
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert not any(t["step"] == "draft" for t in trace)


@pytest.mark.parametrize("status", ["compatible", "conflict"])
def test_irrelevant_retrieved_citations_cannot_create_conflict_or_complete_research(monkeypatch, research_env, status):
    original_tool, original_chat = graph.execute_tool, research_env.chat
    car, airline = corpus.get_clause("3.3.2"), corpus.get_clause("10.2", "INDIGO-COC-DOM")

    def retrieve(name, arguments, claim):
        output = original_tool(name, arguments, claim)
        return [*output, car, airline] if name == "get_definition" else output

    def report(messages, *args, **kwargs):
        reply = original_chat(messages, *args, **kwargs)
        if not reply.get("tool_calls"):
            reply["content"] = json.dumps({"comparison": {
                "status": status, "car_chunk_id": car["chunk_id"], "indigo_chunk_id": airline["chunk_id"],
            }})
        return reply

    monkeypatch.setattr(graph, "execute_tool", retrieve)
    monkeypatch.setattr(research_env, "chat", report)
    result, trace = graph.run_agent({**SCENARIO["claim"], "stated_cause": "weather"})
    retrieved = {chunk_id for step in trace if step["step"] == "tool" for chunk_id in step["chunk_ids"]}
    assert {car["chunk_id"], airline["chunk_id"]} <= retrieved
    assert result.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    assert result.route is Route.ESCALATE and result.amount is None
    assert not any(t["step"] == "draft" for t in trace)


def test_relevant_citations_with_model_conflict_still_require_independent_review(monkeypatch, research_env):
    original = research_env.chat

    def report(messages, *args, **kwargs):
        reply = original(messages, *args, **kwargs)
        if not reply.get("tool_calls"):
            content = json.loads(reply["content"])
            content["comparison"]["status"] = "conflict"
            reply["content"] = json.dumps(content)
        return reply

    monkeypatch.setattr(research_env, "chat", report)
    result, trace = graph.run_agent({**SCENARIO["claim"], "stated_cause": "weather"})
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert result.amount is None
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["checklist"]["airline_comparison"]["status"] == "missing"
    assert draft["route"] is Route.REQUEST_EVIDENCE

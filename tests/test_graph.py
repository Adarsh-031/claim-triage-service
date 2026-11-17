import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from triage_common.cache import CORPUS_VERSION_KEY, run_cache_key
from triage_common.contracts import EscalationReason, Recommendation, Result, Route, RunStatus
from worker import main as worker_main
from worker.agent import graph
from worker.tools import corpus

SCENARIO_DIR = Path(__file__).parents[1] / "eval/scenarios"
SCENARIO = json.loads((SCENARIO_DIR / "s01_demo_cancellation_alternate_taken.json").read_text())

pytestmark = pytest.mark.usefixtures("research_env")


def test_worker_returns_completed_result_from_run_cache(monkeypatch):
    run_id = uuid4()
    cached_result = {
        "route": "escalate",
        "summary": "cached",
        "entitlements": [],
        "compensation_inr": None,
        "citations": [],
        "escalation_reason": "VERIFICATION_FAILED",
        "verified": False,
        "requires_human_approval": True,
    }
    key = run_cache_key(SCENARIO["claim"], "v1", corpus.get_corpus().version, "unknown")

    class FakeCache:
        values = {
            key: json.dumps({"status": "escalated", "result": cached_result}),
            CORPUS_VERSION_KEY: "stale-redis-version",
        }

        def get(self, name):
            return self.values.get(name)

    updates = []
    monkeypatch.setattr(worker_main, "cache_redis", FakeCache())
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))
    monkeypatch.setattr(worker_main, "claim_run", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(worker_main, "heartbeat_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker_main, "finish_run", lambda _rid, _attempt, **fields: (updates.append(fields) or True))
    monkeypatch.setattr(worker_main, "run_agent", lambda *_args, **_kwargs: pytest.fail("cache hit must skip graph"))

    assert worker_main.handle({"run_id": str(run_id), "client_id": "unknown", "prompt_version": "v1"}) is True

    assert updates[-1]["status"] == "escalated"
    assert updates[-1]["result"] == cached_result
    assert updates[-1]["trace"][0]["hit"] is True
    assert updates[-1]["trace"][0]["corpus_version"] == corpus.get_corpus().version


def tool_call(name, **arguments):
    return {"id": "test-tool", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_s01_research_preserves_real_citations_and_is_verified():
    run_id = str(uuid4())
    result, trace = graph.run_agent(SCENARIO["claim"], "test-cid", run_id=run_id)
    assert Result.model_validate_json(result.model_dump_json()) == result
    assert str(result.run_id) == run_id
    # Research is complete and the separate grounding verifier passed the draft.
    assert result.route is Route.DECLINE and result.escalation_reason is None
    assert result.amount == 0
    assert any("Meals and refreshments" in entitlement for entitlement in result.entitlements)
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["route"] == Route.DECLINE
    assert draft["amount"] == 0 and draft["compensation"]["amount"] == 0
    assert draft["checklist"]["airline_comparison"]["status"] == "needs_verification"
    assert "verifier" in draft["checklist"]["airline_comparison"]["reason"]
    assert "compensation declined" in draft["summary"]
    assert "Meals and refreshments are owed" in draft["summary"]
    assert set(SCENARIO["expected"]["must_cite"]) <= {c.clause_id for c in result.citations}
    chunks = corpus.get_corpus().chunks
    for citation in result.citations:
        chunk = chunks[citation.chunk_id]
        assert citation.doc_id == chunk["doc_id"]
        assert citation.quote in chunk["text"]
        assert not citation.chunk_id.startswith("DEMO")
    assert result.prompt_version == "intake-v1+research-v1+v1"
    assert result.corpus_version == corpus.get_corpus().version
    assert trace[0]["cid"] == "test-cid"
    # Only intake and research consume the model step budget; draft/verify are deterministic gates.
    assert trace[-1]["step_count"] == 6
    assert trace[-1]["tool_calls"] == 14
    assert trace[-2]["step"] == "verify" and trace[-2]["verified"] is True
    assert trace[-1]["verified"] is True
    assert {t["name"] for t in trace if t["step"] == "tool"} == {
        "search_corpus", "get_clause", "get_references", "get_referrers", "get_definition",
        "get_claim_field", "compute_compensation",
    }
    assert all(item["status"] in {"cited", "missing", "needs_verification"} for item in draft["checklist"].values())
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
    result, trace = graph.run_agent(SCENARIO["claim"], prompt_version=prompt_version)
    assert result.prompt_version == f"intake-v1+research-v1+{prompt_version}"
    intake = next(t for t in trace if t["step"] == "intake")
    assert intake["prompt_version"] == prompt_version


@pytest.mark.parametrize("scenario_id", [
    "s05_cancel_late_notice_no_alternate", "s29_cancel_late_notice_mid_block_cap",
])
def test_cancellation_gold_amount_matches_calculator_trace(scenario_id):
    scenario = json.loads((SCENARIO_DIR / f"{scenario_id}.json").read_text())
    _, trace = graph.run_agent(scenario["claim"])
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["compensation"]["status"] == "computed"
    assert draft["compensation"]["amount"] == scenario["expected"]["compensation_inr"]


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
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED


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


def _mock_worker_store(monkeypatch):
    updates = []
    monkeypatch.setattr(worker_main, "get_run", lambda _: SimpleNamespace(claim=SCENARIO["claim"]))

    def claim(run_id, *, now):
        updates.append((run_id, {"status": RunStatus.running.value}))
        return 1

    def finish(run_id, attempt, **update):
        assert attempt == 1
        updates.append((run_id, update))
        return True

    monkeypatch.setattr(worker_main, "claim_run", claim)
    monkeypatch.setattr(worker_main, "finish_run", finish)
    monkeypatch.setattr(worker_main, "heartbeat_run", lambda *args, **kwargs: True)
    return updates


@pytest.mark.parametrize("error", [OSError("missing corpus"), ValueError("invalid corpus")])
def test_worker_skips_cache_when_local_corpus_version_is_unknown(monkeypatch, error):
    updates = _mock_worker_store(monkeypatch)
    monkeypatch.setattr(worker_main.settings, "cache_enabled", True)
    monkeypatch.setattr(worker_main, "get_corpus", Mock(side_effect=error))
    cache_read, cache_write = Mock(), Mock()
    monkeypatch.setattr(worker_main, "cache_get", cache_read)
    monkeypatch.setattr(worker_main, "cache_set", cache_write)

    assert worker_main.handle({"run_id": str(uuid4())}) is True

    assert updates[-1][1]["status"] == "done"
    cache_read.assert_not_called()
    cache_write.assert_not_called()


@pytest.mark.parametrize("budget_exhausted", [False, True])
def test_worker_preserves_run_id_and_legacy_api_payload(monkeypatch, budget_exhausted):
    run_id = uuid4()
    updates = _mock_worker_store(monkeypatch)
    original = graph.run_agent

    def checked(claim, correlation_id, *, run_id, prompt_version):
        result, trace = original(claim, correlation_id, run_id=run_id, prompt_version=prompt_version)
        assert str(result.run_id) == run_id
        return result, trace

    monkeypatch.setattr(worker_main, "run_agent", checked)
    if budget_exhausted:
        monkeypatch.setattr(graph.settings, "agent_max_tool_calls", 1)
    monkeypatch.setattr(worker_main.settings, "cache_enabled", False)
    assert worker_main.handle({"run_id": str(run_id), "cid": "worker-test",
                               "prompt_version": "v1"}) is True
    assert updates[0] == (run_id, {"status": RunStatus.running.value})
    assert updates[-1][0] == run_id
    final = updates[-1][1]
    rec = Recommendation.model_validate(final["result"])
    assert rec.requires_human_approval is True
    if budget_exhausted:
        assert final["status"] == "escalated" and rec.verified is False
        assert rec.escalation_reason is EscalationReason.BUDGET_EXCEEDED
    else:
        # The demo claim passes grounding verification and publishes its decline.
        assert final["status"] == "done" and rec.verified is True
        assert rec.route is Route.DECLINE and rec.escalation_reason is None


@pytest.mark.parametrize("route", [Route.DECLINE, Route.PAY, Route.REQUEST_EVIDENCE])
def test_worker_never_publishes_unverified_consequential_draft(monkeypatch, route):
    original = graph.make_draft

    def consequential(state):
        return {**original(state), "route": route, "summary": "Unverified decision", "amount": 1000}

    monkeypatch.setattr(graph, "make_draft", consequential)
    updates = _mock_worker_store(monkeypatch)
    assert worker_main.handle({"run_id": str(uuid4())}) is True
    saved = updates[-1][1]
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

    def report(messages, tools=None, **kwargs):
        reply = original(messages, tools, **kwargs)
        if tools and not reply.get("tool_calls"):  # the research report, not the verifier's verdict
            content = json.loads(reply["content"])
            content["comparison"]["status"] = "conflict"
            reply["content"] = json.dumps(content)
        return reply

    monkeypatch.setattr(research_env, "chat", report)
    result, trace = graph.run_agent({**SCENARIO["claim"], "stated_cause": "weather"})
    # The research model's "conflict" is discarded; the verifier judges the comparison itself.
    draft = next(t for t in trace if t["step"] == "draft")
    assert draft["checklist"]["airline_comparison"]["status"] == "needs_verification"
    verify = next(t for t in trace if t["step"] == "verify")
    assert verify["llm_called"] is True and verify["lower_protection"] is False
    assert result.route is Route.DECLINE and result.amount == 0


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_selected_prompt_content_reaches_provider(monkeypatch, version):
    seen = []

    def chat(messages, tools, **kwargs):
        seen.append(messages[0]["content"])
        raise httpx.ConnectError("stop after observing real provider input")

    monkeypatch.setattr(graph, "get_llm", lambda: SimpleNamespace(chat=chat))
    _, trace = graph.run_agent(SCENARIO["claim"], prompt_version=version)
    expected = (graph.PROMPT_PATH.parent / f"{version}.md").read_text(encoding="utf-8")
    other = (graph.PROMPT_PATH.parent / f"{'v2' if version == 'v1' else 'v1'}.md").read_text(encoding="utf-8")
    assert expected in seen[0] and other not in seen[0]
    intake = next(s for s in trace if s["step"] == "intake")
    assert intake["prompt_version"] == version


def test_prompt_injection_cannot_change_route_or_create_amount():
    scenario_path = Path(__file__).parents[1] / "eval/scenarios/s23_statement_prompt_injection.json"
    claim = json.loads(scenario_path.read_text())["claim"]

    class HostileLLM:
        def chat(self, messages, tools=None, **kwargs):
            return {"content": json.dumps({
                "disruption_type": "delay", "notice_minutes_before_departure": None,
                "block_time_minutes": None, "alternate_offered": None, "alternate_accepted": None,
                "contact_details_given": None, "stated_cause": None,
            })}

    result, trace = graph.run_agent(claim, llm=HostileLLM())
    # Structured facts win: the injected statement neither changes the route nor creates money.
    assert result.route is Route.DECLINE
    assert result.amount == 0
    intake_trace = next(item for item in trace if item["step"] == "intake")
    assert intake_trace["facts"]["disruption_type"] == "cancellation"
    assert "400%" not in str(intake_trace["facts"])


def test_vietnam_route_is_escalated_before_retrieval():
    scenario_path = Path(__file__).parents[1] / "eval/scenarios/s22_vietnam_route_out_of_corpus.json"
    claim = json.loads(scenario_path.read_text())["claim"]
    result, trace = graph.run_agent(claim)
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.OUT_OF_CORPUS
    assert trace[-1]["step"] == "finalize"
    assert not any(entry["step"] == "research" for entry in trace)
    intake = next(item for item in trace if item["step"] == "intake")
    assert intake["corpus_version"] == result.corpus_version == corpus.get_corpus().version


def test_narrative_facts_reach_research_tools_checklist_and_calculator():
    scenario = json.loads((SCENARIO_DIR / "s29_cancel_late_notice_mid_block_cap.json").read_text())
    claim = {**scenario["claim"], "disruption_type": "unknown", "notice_minutes_before_departure": None,
             "block_time_minutes": None, "contact_info_provided": None, "alternate": None}

    class NarrativeLLM:
        def chat(self, messages, **kwargs):
            return {"content": json.dumps({
                "disruption_type": "cancellation", "notice_minutes_before_departure": 120,
                "block_time_minutes": 90, "alternate_offered": False, "alternate_accepted": False,
                "contact_details_given": True,
            })}

    result, trace = graph.run_agent(claim, llm=NarrativeLLM())
    draft = next(item for item in trace if item["step"] == "draft")
    assert draft["compensation"]["status"] == "computed"
    assert draft["compensation"]["amount"] == 7500
    assert draft["checklist"]["notice_period"]["missing_fields"] == []
    assert draft["checklist"]["alternate_offered_accepted"]["missing_fields"] == []
    assert draft["checklist"]["contact_details"]["missing_fields"] == []
    # The amount comes from the calculator on reconciled facts and passes verification.
    assert result.route is Route.PAY and result.amount == 7500
    assert claim["disruption_type"] == "unknown" and claim["alternate"] is None
    assert graph._INTAKE_LLM.get() is None
    json.dumps(trace)


def test_hostile_intake_output_cannot_add_a_route_or_amount():
    class HostileLLM:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return {"content": json.dumps({"disruption_type": "cancellation", "amount": 400000,
                                          "route": "pay_as_computed"})}

    llm = HostileLLM()
    result, trace = graph.run_agent(SCENARIO["claim"], llm=llm)
    assert llm.calls == 2
    assert result.route is Route.ESCALATE and result.amount is None
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert [item["step"] for item in trace] == ["start", "intake", "finalize"]
    assert trace[1]["corpus_version"] == result.corpus_version == corpus.get_corpus().version
    assert graph._INTAKE_LLM.get() is None
    # A subsequent run must not reuse the failing injected provider.
    _, next_trace = graph.run_agent(SCENARIO["claim"])
    assert any(item["step"] == "research" for item in next_trace)


def test_intake_provider_outage_escalates_before_research():
    class Unavailable:
        def chat(self, messages, **kwargs):
            raise httpx.ConnectError("offline")

    result, trace = graph.run_agent(SCENARIO["claim"], llm=Unavailable())
    assert result.escalation_reason is EscalationReason.PROVIDER_DEGRADED
    assert result.amount is None
    assert not any(item["step"] == "research" for item in trace)


@pytest.mark.parametrize("scenario_id", [
    "s03_cancel_notice_under_two_weeks", "s04_cancel_notice_at_least_two_weeks",
    "s07_cancel_extraordinary_circumstances",
])
def test_cancellation_evidence_matches_supplied_facts(scenario_id):
    scenario = json.loads((SCENARIO_DIR / f"{scenario_id}.json").read_text())
    result, trace = graph.run_agent(scenario["claim"])
    assert result.route.value == scenario["expected"]["route"]
    assert set(scenario["expected"]["must_cite"]) <= {c.clause_id for c in result.citations}
    draft = next(t for t in trace if t["step"] == "draft")
    if scenario_id.startswith(("s03", "s04")):
        assert result.amount == 0
        assert draft["compensation"]["inputs_used"]["alternate_or_refund_offered"] is True
        assert "Missing evidence" not in result.summary
    else:
        assert "unavoidability despite reasonable measures" in result.summary

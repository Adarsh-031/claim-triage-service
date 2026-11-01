"""Grounding verifier (#27): one test per check, fabricated quotes, and the retry path."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from triage_common.contracts import EscalationReason, Route
from worker.agent import graph, verifier
from worker.tools import corpus

SCENARIO_DIR = Path(__file__).parents[1] / "eval/scenarios"
S01 = json.loads((SCENARIO_DIR / "s01_demo_cancellation_alternate_taken.json").read_text())
S05 = json.loads((SCENARIO_DIR / "s05_cancel_late_notice_no_alternate.json").read_text())

pytestmark = pytest.mark.usefixtures("research_env")


class FakeLLM:
    """Records calls and answers with a fixed verdict (or raises)."""

    def __init__(self, supported=True, lower_protection=False, raw=None, error=None):
        self.calls, self.supported, self.lower, self.raw, self.error = [], supported, lower_protection, raw, error

    def chat(self, messages, tools=None, *, timeout_s=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        if self.raw is not None:
            return {"content": self.raw}
        payload = json.loads(messages[-1]["content"])
        verdict = {"claims": [{"id": c["id"], "supported": self.supported, "reason": "test"}
                              for c in payload["claims"]]}
        if "comparison" in payload:
            verdict["comparison"] = {"lower_protection": self.lower, "reason": "test comparison"}
        return {"content": json.dumps(verdict)}


def researched(claim):
    """Run the real graph once and return the state the verifier saw on its first attempt."""
    seen = []
    original = verifier.run_checks

    def capture(draft, state, llm, **kwargs):
        seen.append((dict(draft), dict(state)))
        return original(draft, state, llm, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(verifier, "run_checks", capture)
    try:
        result, trace = graph.run_agent(claim)
    finally:
        mp.undo()
    draft, state = seen[0]
    return result, trace, draft, state


@pytest.fixture
def s01(research_env):
    return researched(S01["claim"])


def failures(check, draft, state):
    return check(draft, state)["failures"]


# --- End to end -----------------------------------------------------------------------

def test_s01_demo_is_verified_and_declines_with_meals(s01):
    result, trace, _, _ = s01
    assert result.route is Route.DECLINE and result.amount == 0
    assert set(S01["expected"]["must_cite"]) <= {c.clause_id for c in result.citations}
    assert "Meals and refreshments are owed" in result.summary
    verify = [t for t in trace if t["step"] == "verify"]
    assert len(verify) == 1 and verify[0]["verified"] is True and verify[0]["llm_called"] is True
    assert trace[-1]["step"] == "finalize" and trace[-1]["verified"] is True


def test_s05_late_notice_no_alternate_pays_computed_amount():
    result, trace = graph.run_agent(S05["claim"])
    assert result.route is Route.PAY
    assert result.amount == S05["expected"]["compensation_inr"]
    assert "approve" in result.summary and not verifier.PAYMENT_AUTHORISATION.search(result.summary)


def test_good_draft_passes_every_deterministic_check(s01):
    _, _, draft, state = s01
    for check in verifier.DETERMINISTIC_CHECKS:
        assert check(draft, state)["passed"], check.__name__


# --- One test per deterministic check --------------------------------------------------

def test_citations_retrieved_rejects_a_chunk_not_retrieved_in_this_run(s01):
    _, _, draft, state = s01
    retrieved = {c["chunk_id"] for c in state["retrieved"]}
    other = next(c for key, c in corpus.get_corpus().chunks.items() if key not in retrieved)
    bad = {**draft, "citations": [*draft["citations"], {"chunk_id": other["chunk_id"], "doc_id": other["doc_id"],
                                                        "clause_id": other["clause_id"], "quote": other["text"]}]}
    assert any("not retrieved" in f for f in failures(verifier.citations_retrieved, bad, state))


def test_citations_retrieved_rejects_a_summary_clause_that_is_not_cited(s01):
    _, _, draft, state = s01
    bad = {**draft, "summary": draft["summary"] + " Hotel accommodation is due (CAR 3.4.3)."}
    assert any("3.4.3" in f for f in failures(verifier.citations_retrieved, bad, state))


def test_quotes_grounded_catches_a_fabricated_quote(s01):
    _, _, draft, state = s01
    first = draft["citations"][0]
    bad = {**draft, "citations": [{**first, "quote": first["quote"] + " Compensation is always payable."},
                                  *draft["citations"][1:]]}
    assert failures(verifier.quotes_grounded, bad, state)


def test_quotes_grounded_only_normalises_whitespace(s01):
    _, _, draft, state = s01
    first = draft["citations"][0]
    spaced = {**draft, "citations": [{**first, "quote": "  " + "\n  ".join(first["quote"].split()) + " "}]}
    assert verifier.quotes_grounded(spaced, state)["passed"]
    recased = {**draft, "citations": [{**first, "quote": first["quote"].upper()}]}
    assert not verifier.quotes_grounded(recased, state)["passed"]


def test_checklist_complete_rejects_missing_sources(s01):
    _, _, draft, state = s01
    item = next(iter(state["checklist"]))
    broken = {**state, "checklist": {**state["checklist"],
                                     item: {**state["checklist"][item], "missing_chunks": ["CAR-S3-M-IV-R4:9.9"]}}}
    assert any("missing sources" in f for f in failures(verifier.checklist_complete, draft, broken))


def test_amount_matches_rejects_an_amount_not_from_compute_compensation(s01):
    _, _, draft, state = s01
    assert any("!= computed" in f for f in failures(verifier.amount_matches, {**draft, "amount": 5000}, state))
    evidence = {**draft, "route": Route.REQUEST_EVIDENCE, "amount": 100}
    assert any("must not carry an amount" in f for f in failures(verifier.amount_matches, evidence, state))


def test_route_allowed_rejects_escalate_and_uncited_decisions(s01):
    _, _, draft, state = s01
    assert failures(verifier.route_allowed, {**draft, "route": Route.ESCALATE}, state)
    assert failures(verifier.route_allowed, {**draft, "route": "approve_now"}, state)
    assert failures(verifier.route_allowed, {**draft, "citations": []}, state)


@pytest.mark.parametrize("wording", ["Payment approved.", "We have paid the passenger.",
                                     "This payment has been authorised.", "INR 5000 will be paid today."])
def test_no_payment_authorisation_blocks_approval_wording(s01, wording):
    _, _, draft, state = s01
    assert failures(verifier.no_payment_authorisation, {**draft, "summary": wording}, state)
    assert verifier.no_payment_authorisation(
        {"summary": "A claims officer must approve any payment."}, state)["passed"]


# --- LLM support check ----------------------------------------------------------------

def test_deterministic_failure_short_circuits_before_the_llm(s01):
    _, _, draft, state = s01
    llm = FakeLLM()
    first = draft["citations"][0]
    bad = {**draft, "citations": [{**first, "quote": "invented text"}]}
    outcome = verifier.run_checks(bad, state, llm)
    assert outcome["verified"] is False and outcome["llm_called"] is False and llm.calls == []


def test_llm_check_pairs_each_sentence_with_the_clauses_it_names(s01):
    _, _, draft, state = s01
    pairs = verifier.support_pairs(draft)
    assert {p["clause_id"] for p in pairs} >= {"3.3.1", "3.3.2", "3.8.1(a)"}
    assert all(p["quote"] in corpus.get_corpus().clause(p["clause_id"])["text"] for p in pairs)


def test_llm_unsupported_quote_fails_verification(s01):
    _, _, draft, state = s01
    outcome = verifier.run_checks(draft, state, FakeLLM(supported=False))
    assert outcome["verified"] is False
    assert not next(c for c in outcome["checks"] if c["check"] == "llm_support")["passed"]


def test_llm_malformed_answer_fails_verification(s01):
    _, _, draft, state = s01
    assert verifier.run_checks(draft, state, FakeLLM(raw="yes, looks fine"))["verified"] is False


def test_llm_lower_protection_escalates_as_provision_conflict(monkeypatch):
    real, judge = graph.get_llm(), FakeLLM(lower_protection=True)

    class ResearchThenJudge:
        def chat(self, messages, tools=None, **kwargs):
            return (judge if tools is None else real).chat(messages, tools, **kwargs)

    monkeypatch.setattr(graph, "get_llm", lambda: ResearchThenJudge())
    result, trace = graph.run_agent(S01["claim"])
    assert result.route is Route.ESCALATE and result.escalation_reason is EscalationReason.PROVISION_CONFLICT
    assert {c.doc_id for c in result.citations} >= {corpus.CAR_DOC}
    assert [t["step"] for t in trace].count("verify") == 1  # a conflict is not retried


def test_verifier_provider_outage_escalates_as_degraded(monkeypatch):
    real = graph.get_llm()

    class ResearchThenFail:
        def chat(self, messages, tools=None, **kwargs):
            if tools is None:
                raise httpx.ConnectError("verifier down")
            return real.chat(messages, tools, **kwargs)

    monkeypatch.setattr(graph, "get_llm", lambda: ResearchThenFail())
    result, _ = graph.run_agent(S01["claim"])
    assert result.escalation_reason is EscalationReason.PROVIDER_DEGRADED


# --- Retry path -----------------------------------------------------------------------

def test_fabricated_quote_is_retried_then_escalated_and_never_returned(monkeypatch):
    original = graph.make_draft
    fabricated = "Passengers are always entitled to INR 10000."

    def fabricate(state):
        draft = original(state)
        draft["citations"] = [{**draft["citations"][0], "quote": fabricated}, *draft["citations"][1:]]
        return draft

    monkeypatch.setattr(graph, "make_draft", fabricate)
    result, trace = graph.run_agent(S01["claim"])
    steps = [t["step"] for t in trace]
    # fail -> retry 1 (cross-references) -> fail -> retry 2 (BM25) -> fail -> escalate
    assert steps.count("verify") == 3
    assert [t["strategy"] for t in trace if t["step"] == "verify_retry"] == ["cross_references", "bm25"]
    assert result.route is Route.ESCALATE and result.escalation_reason is EscalationReason.VERIFICATION_FAILED
    assert fabricated not in json.dumps(result.model_dump(mode="json"))
    assert all(any(c.quote in chunk["text"] for chunk in corpus.get_corpus().chunks.values())
               for c in result.citations)
    assert trace[-1]["verified"] is False


def test_retry_recovers_when_expansion_finds_the_missing_clause(monkeypatch):
    original = graph.make_draft
    calls = []

    def first_draft_misses_a_clause(state):
        calls.append(1)
        if len(calls) == 1:
            state["retrieved"] = [c for c in state["retrieved"] if c["clause_id"] != "3.8.1(a)"]
            state["checklist"] = graph.checklist(state, complete=True)
        return original(state)

    monkeypatch.setattr(graph, "make_draft", first_draft_misses_a_clause)
    result, trace = graph.run_agent(S01["claim"])
    retry = next(t for t in trace if t["step"] == "verify_retry")
    assert retry["strategy"] == "cross_references"
    assert any(corpus.get_corpus().chunks[c]["clause_id"] == "3.8.1(a)" for c in retry["chunk_ids"])
    assert result.route is Route.DECLINE
    assert [t["verified"] for t in trace if t["step"] == "verify"][-1] is True


def test_finalize_refuses_a_draft_swapped_after_verification(s01):
    _, _, draft, state = s01
    state = {**state, "verified": True, "verified_draft": dict(draft),
             "draft": {**draft, "summary": "Swapped after verification"}}
    out = graph.finalize(state)
    assert out["result"]["route"] == "escalate"
    assert out["result"]["escalation_reason"] == "VERIFICATION_FAILED"


def test_each_verifier_attempt_is_stored_as_a_row(monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from triage_common import store

    engine = create_engine("sqlite://")
    store.Base.metadata.create_all(engine, tables=[store.VerifierOutcome.__table__])
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    _, trace = graph.run_agent(S01["claim"])
    run_id = uuid4()
    store.record_verifier_outcomes(run_id, 1, [t for t in trace if t["step"] == "verify"])
    with store.SessionLocal() as session:
        rows = session.scalars(select(store.VerifierOutcome)).all()
    assert len(rows) == 1 and rows[0].run_id == run_id and rows[0].verified is True
    assert "quotes_grounded" in rows[0].passed_checks and rows[0].failed_checks == []

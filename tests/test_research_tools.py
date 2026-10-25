import json
from types import SimpleNamespace

import pytest
from worker.agent import graph
from worker.agent.llm import ResilientLLM
from worker.agent.research import CHECKLISTS, checklist, execute_tool, get_claim_field, provision_conflict
from worker.tools import corpus
from worker.tools.compensation import compute_compensation

pytestmark = pytest.mark.usefixtures("research_env")


def test_graph_edges_keep_document_identity_and_unresolved_targets():
    assert set(corpus.get_references("3.3.2")) >= {f"{corpus.CAR_DOC}:3.3.1", f"{corpus.CAR_DOC}:3.8.1(a)"}
    assert f"{corpus.CAR_DOC}:3.3.2" in corpus.get_referrers("3.8.1(a)")
    assert corpus.get_references(f"{corpus.CAR_DOC}:3.3.2") == corpus.get_references("3.3.2")
    assert f"{corpus.CAR_DOC}:3.2" in corpus.get_references("10.2", "INDIGO-COC-DOM")
    assert "INDIGO-COC-DOM:10.2(c)(i)" in corpus.get_references("10.2", "INDIGO-COC-DOM")
    assert corpus.get_clause("INDIGO-COC-DOM:10.2(c)(i)") is None
    assert corpus.get_clause("3.3.2")["doc_id"] == corpus.CAR_DOC
    assert corpus.get_references("no-such-clause") == []
    assert corpus.get_referrers("no-such-clause") == []


def test_definition_lookup_reads_real_artifacts_and_preserves_source_quotes():
    chunks = corpus.get_definition(" Force MAJEURE ")
    assert {c["doc_id"] for c in chunks} == {corpus.CAR_DOC, "INDIGO-COC-DOM", "INDIGO-COC-INTL"}
    assert all(c["text"] == corpus.get_corpus().chunks[c["chunk_id"]]["text"] for c in chunks)
    assert len(corpus.get_definition("force majeure", "INDIGO-COC-DOM")) == 1
    assert corpus.get_definition("imaginary term") == []


def test_search_filters_limits_and_uses_processed_chunks():
    results = corpus.search_corpus("cancellation", k=2, doc_id=corpus.CAR_DOC)
    assert len(results) == 2
    assert all(c["doc_id"] == corpus.CAR_DOC for c in results)
    assert corpus.search_corpus("cancellation", k=0) == []
    assert corpus.search_corpus("cancellation", doc_id="missing") == []
    assert corpus.search_corpus("zzzzzz") == []


def test_claim_fields_preserve_false_zero_and_missing():
    claim = {"contact_info_provided": False, "notice_minutes_before_departure": 0,
             "alternate": {"offered": True, "accepted": None}}
    assert get_claim_field(claim, "contact_info_provided") is False
    assert get_claim_field(claim, "notice_minutes_before_departure") == 0
    assert get_claim_field(claim, "alternate.accepted") is None
    with pytest.raises(ValueError):
        get_claim_field(claim, "statement")
    with pytest.raises(ValueError):
        get_claim_field(claim, "system_prompt")


def test_calculator_uses_claim_inputs_and_never_total_ticket_fare():
    claim = {"notice_minutes_before_departure": 90, "contact_info_provided": True,
             "alternate": {"offered": True, "accepted": False}, "block_time_minutes": 165,
             "ticket_fare_inr": 12000}
    result = execute_tool("compute_compensation", {"clause_id": "3.3.2"}, claim)
    assert result["status"] == "needs_evidence" and result["amount"] is None
    assert result["missing_fields"] == ["one_way_basic_fare_inr", "airline_fuel_charge_inr"]
    claim["alternate"]["accepted"] = True
    result = execute_tool("compute_compensation", {"clause_id": "3.3.2"}, claim)
    assert result["status"] == "computed" and result["amount"] == 0
    assert result["formula"] == "alternate accepted, 3.3.2"
    with pytest.raises(ValueError):
        execute_tool("compute_compensation", {"clause_id": "3.3.2", "inputs": {"amount": 50000}}, claim)


@pytest.mark.parametrize("block,cap,clause", [(60, 5000, "a"), (61, 7500, "b"), (120, 7500, "b"), (121, 10000, "c")])
@pytest.mark.parametrize("basic", [2000, 20000])
def test_cancellation_adapter_caps_and_boundaries(block, cap, clause, basic):
    inputs = {"notice_minutes_before_departure": 90, "alternate_flight_accepted": False,
              "contact_info_provided": True, "block_time_minutes": block,
              "one_way_basic_fare_inr": basic, "airline_fuel_charge_inr": 100}
    result = compute_compensation("3.3.2", inputs)
    assert result["amount"] == min(cap, basic + 100)
    assert result["clause_id"] == f"3.3.2({clause})"


@pytest.mark.parametrize("notice", [1440, 20160])
def test_timely_notice_needs_acceptable_arrangement(notice):
    inputs = {"notice_minutes_before_departure": notice, "alternate_flight_accepted": False,
              "contact_info_provided": True}
    assert compute_compensation("3.3.2", inputs)["missing_fields"] == ["alternate_or_refund_offered"]
    result = compute_compensation("3.3.2", {**inputs, "alternate_or_refund_offered": True})
    assert result["amount"] == 0 and result["clause_id"] == "3.3.1"


def test_all_disruptions_have_checklists_and_missing_evidence_is_explicit():
    assert set(CHECKLISTS) == {"cancellation", "delay", "denied_boarding", "downgrade"}
    for disruption in CHECKLISTS:
        items = checklist({"claim": {"disruption_type": disruption}, "retrieved": []}, complete=True)
        assert items
        assert all(item["status"] == "missing" and item["missing_chunks"] for item in items.values())


def test_conflict_requires_citations_from_both_documents():
    car = corpus.get_clause("1.4")
    airline = corpus.get_clause("1.1", "INDIGO-COC-DOM")
    state = {"claim": {"airline_code": "6E", "stated_cause": "technical fault"}, "retrieved": [car]}
    assert provision_conflict(state) == []
    state["retrieved"].append(airline)
    assert provision_conflict(state) == [car["chunk_id"], airline["chunk_id"]]
    state["claim"]["stated_cause"] = "weather"
    assert provision_conflict(state) == []
    state["research_report"] = {"comparison": {"status": "conflict", "car_chunk_id": car["chunk_id"],
                                              "indigo_chunk_id": "fabricated"}}
    assert provision_conflict(state) == []


def test_corpus_rejects_tampered_source(tmp_path, monkeypatch, processed_corpus):
    for name in corpus.ARTIFACTS:
        (tmp_path / name).write_bytes((processed_corpus / name).read_bytes())
    path = tmp_path / "chunks.jsonl"
    chunks = [json.loads(line) for line in path.read_text().splitlines()]
    chunks[0]["text"] = "Changed without updating the hash"
    path.write_text("\n".join(json.dumps(c) for c in chunks))
    monkeypatch.setattr(corpus.settings, "corpus_dir", str(tmp_path))
    with pytest.raises(ValueError, match="hash mismatch"):
        corpus.get_corpus()


def test_resilient_provider_does_not_retry_beyond_run_deadline(monkeypatch):
    import httpx
    from worker.agent import llm

    now = [100.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    calls = []

    def fail(messages, tools, *, timeout_s):
        calls.append(timeout_s)
        now[0] += timeout_s
        raise httpx.TimeoutException("slow provider")

    fallback_calls = []
    fallback = SimpleNamespace(chat=lambda *a, **kw: fallback_calls.append(1))
    resilient = ResilientLLM(SimpleNamespace(chat=fail), fallback)
    with pytest.raises(httpx.TimeoutException):
        resilient.chat([], [], timeout_s=2)
    assert calls == [2] and fallback_calls == []
    assert now[0] == 102


def test_narrative_is_withheld_from_provider(research_env, monkeypatch):
    original = research_env.chat
    contents = []

    def inspect(messages, *args, **kwargs):
        contents.extend(m.get("content", "") for m in messages if m.get("role") == "user")
        return original(messages, *args, **kwargs)

    monkeypatch.setattr(research_env, "chat", inspect)
    graph.run_agent({"disruption_type": "unknown", "statement": "UNTRUSTED_INSTRUCTION_SENTINEL"})
    assert all("UNTRUSTED_INSTRUCTION_SENTINEL" not in content for content in contents)

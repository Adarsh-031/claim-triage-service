"""S14–S18 exercise narrative intake through source-backed drafting."""

import json
from pathlib import Path

import pytest
from triage_common.contracts import ClaimIn
from worker.agent.intake import extract_intake, reconcile_claim
from worker.agent.research import checklist, execute_tool, make_draft, research_facts
from worker.tools import corpus

ROOT = Path(__file__).parents[1]
FACTS = json.loads((ROOT / "tests/fixtures/intake_scenarios.json").read_text())


class NarrativeLLM:
    def __init__(self, facts):
        self.facts = facts

    def chat(self, messages):
        return {"content": json.dumps(self.facts)}


@pytest.mark.usefixtures("research_env")
@pytest.mark.parametrize("number,amount", [(14, 0), (15, 10000), (16, 20000), (17, None), (18, None)])
def test_denied_boarding_scenario_routes(number, amount):
    scenario = json.loads(next((ROOT / "eval/scenarios").glob(f"s{number}_*.json")).read_text())
    claim = ClaimIn.model_validate(scenario["claim"]).model_dump(mode="json")
    facts = extract_intake(claim, llm=NarrativeLLM(FACTS[scenario["id"]]))
    claim = reconcile_claim(claim, facts)
    assert research_facts(claim)["involuntary_denial"] is True
    state = {"claim": claim, "retrieved": list(corpus.get_corpus().chunks.values())}
    state["checklist"] = checklist(state, complete=True)
    state["compensation"] = execute_tool("compute_compensation", {"clause_id": "3.2.2"}, claim)
    draft = make_draft(state)
    assert draft["route"] == scenario["expected"]["route"]
    assert draft.get("amount") == amount
    assert set(scenario["expected"]["must_cite"]) <= {c["clause_id"] for c in draft["citations"]}
    if number == 16:
        assert "alternate.departure_delay_minutes" not in state["checklist"]["alternate"]["missing_fields"]
        assert draft["entitlements"] == ["Full ticket refund (CAR 3.2.2(c))"]
    if number == 18:
        assert draft["escalation_reason"] == "PROVISION_CONFLICT"


@pytest.mark.parametrize("field", ["involuntary_denial", "signed_voucher_agreement", "voucher_issued"])
def test_denial_and_voucher_form_conflicts_are_preserved(field):
    claim = {"disruption_type": "denied_boarding", field: False}
    facts = extract_intake(claim, llm=NarrativeLLM({"disruption_type": "denied_boarding", field: True}))
    assert getattr(facts, field) is False
    assert field in facts.conflicts[0]
    assert reconcile_claim(claim, facts)[field] is False


@pytest.mark.usefixtures("research_env")
@pytest.mark.parametrize("number", range(14, 19))
def test_denied_boarding_graph_routes(number):
    from worker.agent import graph

    scenario = json.loads(next((ROOT / "eval/scenarios").glob(f"s{number}_*.json")).read_text())

    class IntakeLLM(NarrativeLLM):
        def chat(self, messages, *, timeout_s=None):
            return super().chat(messages)

    result, trace = graph.run_agent(scenario["claim"], llm=IntakeLLM(FACTS[scenario["id"]]))
    assert result.route == scenario["expected"]["route"], trace
    assert result.escalation_reason == scenario["expected"].get("escalation_reason")
    assert set(scenario["expected"]["must_cite"]) <= {c.clause_id for c in result.citations}

"""S19 preserves explicit downgrade facts through intake and verified calculation."""

import json
from pathlib import Path

import pytest
from triage_common.contracts import ClaimIn, Route
from worker.agent import graph
from worker.agent.intake import LocalStructuredStubLLM, extract_intake, reconcile_claim

ROOT = Path(__file__).parents[1]
SCENARIO = json.loads((ROOT / 'eval/scenarios/s19_involuntary_domestic_downgrade.json').read_text())


@pytest.mark.usefixtures('research_env')
def test_s19_intake_to_verified_payment():
    claim = ClaimIn.model_validate(SCENARIO['claim']).model_dump(mode='json')
    facts = extract_intake(claim, llm=LocalStructuredStubLLM())
    normalized = reconcile_claim(claim, facts)
    assert facts.involuntary_downgrade is True
    assert normalized['sector'] == 'domestic'
    assert normalized['ticket_cost_including_taxes_inr'] == 18000
    result, trace = graph.run_agent(normalized)
    calculation = next(item for item in trace if item['step'] == 'draft')['compensation']
    assert calculation['status'] == 'computed'
    assert calculation['inputs_used']['ticket_cost_including_taxes_inr'] == '18000.0'
    assert calculation['inputs_used']['sector'] == 'domestic'
    assert result.route == SCENARIO['expected']['route']
    assert result.amount == 13500
    assert trace[-1]["verified"] is True
    assert SCENARIO['expected']['must_include_entitlement'] in result.entitlements
    assert '3.5.1' in {citation.clause_id for citation in result.citations}


@pytest.mark.usefixtures('research_env')
@pytest.mark.parametrize('field', ['involuntary_downgrade', 'sector', 'ticket_cost_including_taxes_inr'])
def test_downgrade_missing_facts_request_evidence(field):
    claim = {**SCENARIO['claim'], field: None}
    result, trace = graph.run_agent(claim, llm=LocalStructuredStubLLM())
    assert result.route is Route.REQUEST_EVIDENCE, trace
    assert result.amount is None
    assert field in result.summary


@pytest.mark.usefixtures('research_env')
def test_voluntary_downgrade_does_not_recommend_payment():
    result, trace = graph.run_agent({**SCENARIO['claim'], 'involuntary_downgrade': False})
    assert result.route is Route.REQUEST_EVIDENCE, trace
    assert result.amount is None


def test_downgrade_form_conflict_is_preserved():
    class NarrativeLLM:
        def chat(self, messages):
            return {'content': json.dumps({'disruption_type': 'downgrade', 'involuntary_downgrade': True})}

    claim = {**SCENARIO['claim'], 'involuntary_downgrade': False}
    facts = extract_intake(claim, llm=NarrativeLLM())
    assert facts.involuntary_downgrade is False
    assert any('involuntary_downgrade' in conflict for conflict in facts.conflicts)

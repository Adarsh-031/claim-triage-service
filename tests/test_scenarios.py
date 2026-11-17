import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from triage_common.contracts import ClaimIn, EscalationReason

from cli.ingest import parse_markdown

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "eval" / "scenarios"
SCENARIO_FILES = sorted(SCENARIO_DIR.glob("s*.json"))
VALID_ROUTES = {"pay_as_computed", "request_further_evidence", "decline", "escalate", "quarantine"}


def load_scenario(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_scenario_inventory_has_at_least_twenty_unique_ids_and_expected_metadata():
    scenarios = [load_scenario(path) for path in SCENARIO_FILES]
    ids = [scenario.get("id") for scenario in scenarios]

    assert len(scenarios) >= 20
    assert len(ids) == len(set(ids))
    for path, scenario in zip(SCENARIO_FILES, scenarios, strict=True):
        assert scenario["id"] == path.stem
        assert scenario["description"].strip()
        expected = scenario["expected"]
        assert expected["route"] in VALID_ROUTES
        assert isinstance(expected["must_cite"], list)
        assert all(isinstance(clause_id, str) and clause_id for clause_id in expected["must_cite"])
        if expected["route"] == "escalate":
            assert expected.get("escalation_reason") in {reason.value for reason in EscalationReason}
        else:
            assert "escalation_reason" not in expected


def test_indigo_clause_citations_match_ingested_snapshot_ids():
    source_files = [
        (ROOT / "corpus" / "raw" / "indigo-coc-domestic.md", "INDIGO-COC-DOM"),
        (ROOT / "corpus" / "raw" / "indigo-coc-international.md", "INDIGO-COC-INTL"),
    ]
    indigo_clause_ids = {
        chunk.clause_id
        for path, doc_id in source_files
        for chunk in parse_markdown(path.read_text(encoding="utf-8"), doc_id)
    }
    expected_indigo_ids = {
        clause_id
        for path in SCENARIO_FILES
        for clause_id in load_scenario(path)["expected"]["must_cite"]
        if clause_id.startswith("10.")
    }

    assert expected_indigo_ids <= indigo_clause_ids


def test_computed_payment_scenarios_include_a_fare_input():
    for path in SCENARIO_FILES:
        scenario = load_scenario(path)
        claim = scenario["claim"]
        if (
            scenario["expected"]["route"] == "pay_as_computed"
            and claim["disruption_type"] in {"cancellation", "denied_boarding"}
        ):
            assert claim.get("ticket_fare_inr") is not None, path.name


def test_downgrade_payment_scenarios_include_explicit_eligibility_and_cost():
    for path in SCENARIO_FILES:
        scenario = load_scenario(path)
        claim = scenario["claim"]
        if scenario["expected"]["route"] == "pay_as_computed" and claim["disruption_type"] == "downgrade":
            assert claim.get("involuntary_downgrade") is True, path.name
            assert claim.get("sector") in {"domestic", "international"}, path.name
            assert claim.get("ticket_cost_including_taxes_inr") is not None, path.name
            if claim["sector"] == "international":
                assert claim.get("distance_km") is not None, path.name


@pytest.mark.parametrize("scenario_path", SCENARIO_FILES, ids=lambda path: path.stem)
def test_scenario_claims_match_contract_or_are_expected_to_quarantine(scenario_path: Path):
    scenario = load_scenario(scenario_path)
    expected_route = scenario["expected"]["route"]

    if expected_route == "quarantine":
        with pytest.raises(ValidationError):
            ClaimIn.model_validate(scenario["claim"])
    else:
        ClaimIn.model_validate(scenario["claim"])

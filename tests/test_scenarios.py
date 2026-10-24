import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from triage_common.contracts import ClaimIn, EscalationReason

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


@pytest.mark.parametrize("scenario_path", SCENARIO_FILES, ids=lambda path: path.stem)
def test_scenario_claims_match_contract_or_are_expected_to_quarantine(scenario_path: Path):
    scenario = load_scenario(scenario_path)
    expected_route = scenario["expected"]["route"]

    if expected_route == "quarantine":
        with pytest.raises(ValidationError):
            ClaimIn.model_validate(scenario["claim"])
    else:
        ClaimIn.model_validate(scenario["claim"])

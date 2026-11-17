"""Provision-specific delay decisions, independent of provider wording."""
import json
from pathlib import Path

import pytest
from triage_common.contracts import ClaimIn, Route
from worker.agent.intake import extract_intake, reconcile_claim
from worker.agent.research import checklist, make_draft
from worker.tools import corpus

pytestmark = pytest.mark.usefixtures("research_env")
SCENARIOS = Path(__file__).parents[1] / "eval/scenarios"


def draft(claim, omit=()):
    chunks = [c for c in corpus.get_corpus().chunks.values() if c["clause_id"] not in omit]
    state = {"claim": claim, "retrieved": chunks}
    state["checklist"] = checklist(state, complete=True)
    return make_draft(state), state["checklist"]


def claim_for(prefix):
    return json.loads(next(SCENARIOS.glob(prefix + "_*.json")).read_text())["claim"]


@pytest.mark.parametrize("prefix,meals,hotel", [
    ("s08", True, False), ("s09", False, False), ("s10", True, False),
    ("s12", True, True), ("s13", True, True), ("s25", True, False),
])
def test_scenario_facilities(prefix, meals, hotel):
    claim = ClaimIn.model_validate(claim_for(prefix)).model_dump(mode="json")
    result, _ = draft(claim)
    assert result["route"] == Route.DECLINE
    assert any("Meals" in e for e in result["entitlements"]) == meals
    assert any("Hotel" in e for e in result["entitlements"]) == hotel
    assert result["amount"] is None


@pytest.mark.parametrize("block,threshold", [(150, 120), (151, 180), (300, 180), (301, 240)])
@pytest.mark.parametrize("offset", [-1, 0])
def test_meal_thresholds(block, threshold, offset):
    claim = {**claim_for("s08"), "block_time_minutes": block, "delay_minutes": threshold + offset}
    result, _ = draft(claim)
    assert bool(result["entitlements"]) == (offset == 0)


@pytest.mark.parametrize("delay,options", [(359, False), (360, False), (361, True)])
def test_six_hour_boundary(delay, options):
    result, checks = draft({**claim_for("s25"), "sector": "domestic", "delay_minutes": delay})
    assert any("refund" in e for e in result["entitlements"]) == options
    assert ("alternate.offered" in checks["alternate_refund"]["missing_fields"]) == options
    assert any("Meals" in e for e in result["entitlements"])
    assert result["route"] == Route.DECLINE


@pytest.mark.parametrize("time,hotel", [("19:59", False), ("20:00", True), ("03:00", True), ("03:01", False)])
def test_overnight_window(time, hotel):
    result, _ = draft({**claim_for("s12"), "scheduled_departure_local": time})
    assert any("Hotel" in e for e in result["entitlements"]) == hotel


def test_missing_alternate_cannot_block_hotel_or_meals():
    result, checks = draft({**claim_for("s13"), "sector": "domestic", "alternate": None})
    assert "alternate.offered" in checks["alternate_refund"]["missing_fields"]
    assert checks["hotel_facilities"]["missing_fields"] == []
    assert any("Hotel" in e for e in result["entitlements"])
    assert any("Meals" in e for e in result["entitlements"])


def test_unknown_sector_and_time_are_not_inferred():
    result, checks = draft({**claim_for("s10"), "sector": None, "scheduled_departure_local": None})
    assert checks["alternate_refund"]["missing_fields"] == ["sector"]
    assert checks["hotel_facilities"]["missing_fields"] == ["scheduled_departure_local"]
    assert not any("refund" in e or "Hotel" in e for e in result["entitlements"])


def test_narrative_delay_facts_are_reconciled():
    class Provider:
        def chat(self, *args, **kwargs):
            return {"content": json.dumps({"disruption_type": "delay", "sector": "international",
                                           "scheduled_departure_local": "22:00"})}

    claim = {**claim_for("s12"), "sector": None, "scheduled_departure_local": None}
    normalized = reconcile_claim(claim, extract_intake(claim, llm=Provider()))
    assert normalized["sector"] == "international"
    assert normalized["scheduled_departure_local"] == "22:00"


@pytest.mark.parametrize("delay,time,hotel", [
    (360, "22:00", False), (361, "22:00", True),
    (1440, "12:00", False), (1441, "12:00", True),
])
def test_strict_hotel_delay_thresholds(delay, time, hotel):
    result, _ = draft({**claim_for("s13"), "delay_minutes": delay, "scheduled_departure_local": time})
    assert any("Hotel" in e for e in result["entitlements"]) == hotel


def test_missing_checkin_does_not_block_hotel():
    result, checks = draft({**claim_for("s13"), "reported_at_airport": None})
    assert checks["delay_threshold"]["missing_fields"] == ["reported_at_airport"]
    assert any("Hotel" in e for e in result["entitlements"])
    assert not any("Meals" in e for e in result["entitlements"])


def test_extraordinary_cause_needs_proof_before_facilities_are_declared_due():
    result, _ = draft({**claim_for("s13"), "stated_cause": "weather"})
    assert result["entitlements"] == []
    assert "reasonable measures" in result["summary"]


def test_missing_legal_source_cannot_support_decline():
    result, checks = draft(claim_for("s08"), omit=("3.4.1",))
    assert checks["delay_threshold"]["missing_chunks"]
    assert result["route"] == Route.REQUEST_EVIDENCE


def test_international_delay_does_not_require_alternate_facts():
    result, checks = draft(claim_for("s13"))
    assert checks["alternate_refund"]["missing_fields"] == []
    assert not any("refund" in e for e in result["entitlements"])

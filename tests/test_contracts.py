import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from triage_common.contracts import ClaimIn

ROOT = Path(__file__).resolve().parents[1]


def test_demo_claim_valid():
    s = json.loads((ROOT / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text())
    c = ClaimIn(**s["claim"])
    assert c.alternate.accepted is True


def test_malformed_claim_rejected():
    s = json.loads((ROOT / "eval/scenarios/s02_malformed_quarantine.json").read_text())
    with pytest.raises(ValidationError) as e:
        ClaimIn(**s["claim"])
    fields = {".".join(map(str, err["loc"])) for err in e.value.errors()}
    assert "pnr" in fields


def test_accepted_without_offer_rejected():
    s = json.loads((ROOT / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text())
    s["claim"]["alternate"] = {"offered": False, "accepted": True}
    with pytest.raises(ValidationError):
        ClaimIn(**s["claim"])


def test_extra_fields_forbidden():
    s = json.loads((ROOT / "eval/scenarios/s01_demo_cancellation_alternate_taken.json").read_text())
    s["claim"]["approve_payment"] = True
    with pytest.raises(ValidationError):
        ClaimIn(**s["claim"])

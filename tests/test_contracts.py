import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from triage_common.contracts import (
    Chunk,
    ClaimIn,
    Citation,
    EscalationReason,
    Result,
    Route,
    RunState,
)

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


def test_chunk_example_validates():
    example = json.loads((ROOT / "docs/examples/chunk.json").read_text())
    chunk = Chunk.model_validate(example)
    assert chunk.clause_id == "3.3.2"
    assert len(chunk.hash) == 64


def test_citation_example_validates():
    example = json.loads((ROOT / "docs/examples/citation.json").read_text())
    citation = Citation.model_validate(example)
    assert citation.doc_id == "CAR-S3-M-IV-R4"


def test_result_example_validates():
    example = json.loads((ROOT / "docs/examples/result.json").read_text())
    result = Result.model_validate(example)
    assert isinstance(result.run_id, UUID)
    assert result.route is Route.PAY
    assert result.amount == 10000


def test_route_serializes_as_strings():
    assert Route.PAY.value == "pay_as_computed"
    assert json.loads(Route.PAY.model_dump_json()) == "pay_as_computed"


def test_all_required_escalation_reasons_exist():
    expected = {
        "CAUSE_DISPUTED", "PROVISION_CONFLICT", "OUT_OF_CORPUS",
        "VERIFICATION_FAILED", "BUDGET_EXCEEDED", "PROVIDER_DEGRADED",
        "GUARDRAIL_BLOCKED", "WORKER_FAILURE",
    }
    assert expected <= {reason.value for reason in EscalationReason}


def test_run_state_accepts_partial_graph_state():
    state: RunState = {"run_id": "run-1", "route": Route.ESCALATE, "errors": []}
    assert state["route"] is Route.ESCALATE

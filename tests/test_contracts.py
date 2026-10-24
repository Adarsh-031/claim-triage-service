import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from triage_common.contracts import (
    Chunk,
    Citation,
    ClaimIn,
    EscalationReason,
    Result,
    Route,
    RunState,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_RESULT = {
    "run_id": "f0e1d2c3-b4a5-4678-9012-abcdefabcdef",
    "route": Route.PAY.value,
    "summary": "Illustrative recommendation for testing.",
    "citations": [
        {
            "doc_id": "CAR-S3-M-IV-R4",
            "clause_id": "3.3.2",
            "quote": "Example source passage used for retrieval.",
            "chunk_id": "CAR-S3-M-IV-R4:3.3.2:0001",
        }
    ],
    "amount": 10000,
    "escalation_reason": None,
    "prompt_version": "v1",
    "corpus_version": "2026-10-24",
}


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
    assert chunk.chunk_id == "CAR-S3-M-IV-R4:3.3.2:0001"
    assert chunk.clause_id == "3.3.2"
    assert len(chunk.hash) == 64


def test_chunk_id_is_required():
    example = json.loads((ROOT / "docs/examples/chunk.json").read_text())
    example.pop("chunk_id")
    with pytest.raises(ValidationError):
        Chunk.model_validate(example)


def test_citation_example_validates():
    example = json.loads((ROOT / "docs/examples/citation.json").read_text())
    citation = Citation.model_validate(example)
    assert citation.doc_id == "CAR-S3-M-IV-R4"
    assert citation.chunk_id


def test_citation_chunk_id_is_required():
    example = json.loads((ROOT / "docs/examples/citation.json").read_text())
    example.pop("chunk_id")
    with pytest.raises(ValidationError):
        Citation.model_validate(example)


def test_result_example_validates():
    example = json.loads((ROOT / "docs/examples/result.json").read_text())
    result = Result.model_validate(example)
    assert isinstance(result.run_id, UUID)
    assert result.route is Route.PAY
    assert result.amount == 10000


def test_pay_requires_amount():
    payload = {**BASE_RESULT, "amount": None}
    with pytest.raises(ValidationError, match="amount is required"):
        Result.model_validate(payload)


def test_pay_requires_at_least_one_citation():
    payload = {**BASE_RESULT, "citations": []}
    with pytest.raises(ValidationError, match="at least one citation"):
        Result.model_validate(payload)


def test_pay_with_amount_and_citation_is_valid():
    result = Result.model_validate(BASE_RESULT)
    assert result.route is Route.PAY
    assert result.amount == 10000
    assert len(result.citations) >= 1


def test_escalate_requires_reason():
    payload = {**BASE_RESULT, "route": Route.ESCALATE.value, "amount": None, "escalation_reason": None}
    with pytest.raises(ValidationError, match="escalation_reason is required"):
        Result.model_validate(payload)


def test_escalate_with_reason_is_valid():
    payload = {
        **BASE_RESULT,
        "route": Route.ESCALATE.value,
        "amount": None,
        "escalation_reason": EscalationReason.VERIFICATION_FAILED.value,
    }
    result = Result.model_validate(payload)
    assert result.route is Route.ESCALATE
    assert result.escalation_reason is EscalationReason.VERIFICATION_FAILED


@pytest.mark.parametrize("route", [Route.PAY, Route.DECLINE, Route.REQUEST_EVIDENCE])
def test_non_escalate_route_rejects_escalation_reason(route):
    payload = {**BASE_RESULT, "route": route.value, "escalation_reason": "VERIFICATION_FAILED"}
    if route is Route.DECLINE:
        payload["amount"] = None
    with pytest.raises(ValidationError, match="only allowed when route is ESCALATE"):
        Result.model_validate(payload)


def test_decline_requires_at_least_one_citation():
    payload = {**BASE_RESULT, "route": Route.DECLINE.value, "amount": None, "citations": []}
    with pytest.raises(ValidationError, match="at least one citation"):
        Result.model_validate(payload)


def test_decline_with_citation_is_valid():
    payload = {**BASE_RESULT, "route": Route.DECLINE.value, "amount": None}
    result = Result.model_validate(payload)
    assert result.route is Route.DECLINE
    assert len(result.citations) >= 1


def test_route_serializes_as_strings():
    assert Route.PAY.value == "pay_as_computed"
    assert json.loads(json.dumps(Route.PAY)) == "pay_as_computed"


def test_all_required_escalation_reasons_exist():
    expected = {
        "CAUSE_DISPUTED", "PROVISION_CONFLICT", "OUT_OF_CORPUS",
        "VERIFICATION_FAILED", "BUDGET_EXCEEDED", "PROVIDER_DEGRADED",
        "GUARDRAIL_BLOCKED", "WORKER_FAILURE",
    }
    assert expected <= {reason.value for reason in EscalationReason}


def test_run_state_accepts_partial_graph_state():
    state: RunState = {"claim": {"pnr": "AB12CD"}, "route": Route.ESCALATE, "trace": []}
    assert state["route"] is Route.ESCALATE

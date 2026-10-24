"""Versioned data contracts (D1). Every external interface validates against these."""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0"


class DisruptionType(StrEnum):
    cancellation = "cancellation"
    delay = "delay"
    denied_boarding = "denied_boarding"
    downgrade = "downgrade"
    unknown = "unknown"


class AlternateFlight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offered: bool
    accepted: bool | None = None
    departure_delay_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)

    @model_validator(mode="after")
    def accepted_requires_offer(self) -> AlternateFlight:
        if self.accepted and not self.offered:
            raise ValueError("alternate.accepted cannot be true when alternate.offered is false")
        return self


class ClaimIn(BaseModel):
    """POST /v1/claims request body."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    pnr: str = Field(pattern=r"^[A-Z0-9]{6}$")
    airline_code: str = Field(pattern=r"^[A-Z0-9]{2}$", description="IATA code, e.g. 6E")
    flight_number: str = Field(pattern=r"^[A-Z0-9]{2}\s?\d{1,4}$")
    flight_date: date
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    block_time_minutes: int | None = Field(default=None, ge=15, le=600)
    disruption_type: DisruptionType = DisruptionType.unknown
    notice_minutes_before_departure: int | None = Field(default=None, ge=0, le=365 * 24 * 60)
    delay_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    reported_at_airport: bool | None = None
    contact_info_provided: bool | None = None
    stated_cause: str | None = Field(default=None, max_length=300)
    alternate: AlternateFlight | None = None
    ticket_fare_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    statement: str = Field(min_length=10, max_length=4000, description="Passenger's own account")

    @model_validator(mode="after")
    def cross_field(self) -> ClaimIn:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.disruption_type == DisruptionType.delay and self.delay_minutes is None:
            raise ValueError("delay_minutes is required when disruption_type is 'delay'")
        if self.disruption_type in (DisruptionType.cancellation, DisruptionType.denied_boarding) \
                and self.delay_minutes:
            raise ValueError("delay_minutes does not apply to cancellation or denied_boarding")
        return self


class Route(StrEnum):
    pay_as_computed = "pay_as_computed"
    request_further_evidence = "request_further_evidence"
    decline = "decline"
    escalate = "escalate"


class EscalationReason(StrEnum):
    disputed_cause = "DISPUTED_CAUSE"
    provision_conflict = "PROVISION_CONFLICT"
    out_of_corpus = "OUT_OF_CORPUS"
    verification_failed = "VERIFICATION_FAILED"
    budget_exhausted = "BUDGET_EXHAUSTED"
    provider_degraded = "PROVIDER_DEGRADED"
    guardrail_triggered = "GUARDRAIL_TRIGGERED"


class Citation(BaseModel):
    doc_id: str            # e.g. "CAR-S3-M-IV-R4"
    clause_id: str         # e.g. "3.3.2"
    quote: str             # exact retrieved text the claim rests on
    chunk_id: str          # must resolve to a chunk retrieved in this run


class Recommendation(BaseModel):
    route: Route
    summary: str
    entitlements: list[str] = []
    compensation_inr: float | None = None   # computed by code, never by the LLM
    citations: list[Citation] = []
    escalation_reason: EscalationReason | None = None
    verified: bool = False
    requires_human_approval: Literal[True] = True


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    escalated = "escalated"
    failed = "failed"


class RunOut(BaseModel):
    run_id: UUID
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    result: Recommendation | None = None
    error: str | None = None


class SubmitOut(BaseModel):
    run_id: UUID
    status: RunStatus
    poll_url: str


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agree: bool
    note: str | None = Field(default=None, max_length=2000)

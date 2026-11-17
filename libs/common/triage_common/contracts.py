"""Versioned data contracts (D1). Every external interface validates against these."""
from __future__ import annotations

import unicodedata
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0"


def _reject_control_or_zero_width(value: str | None) -> str | None:
    """Reject non-printing control/format characters while allowing normal multiline text."""
    if value is None:
        return value
    for char in value:
        category = unicodedata.category(char)
        # Newline, carriage return, and tab are normal passenger-statement whitespace.
        if category in {"Cc", "Cf", "Cs"} and ord(char) not in {9, 10, 13}:
            raise ValueError("must not contain control or zero-width characters")
    return value


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
    notice_minutes_before_departure: int | None = Field(default=None, ge=0, le=365 * 24 * 60, validate_default=True)
    delay_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60, validate_default=True)
    reported_at_airport: bool | None = None
    involuntary_denial: bool | None = None
    involuntary_downgrade: bool | None = None
    signed_voucher_agreement: bool | None = None
    voucher_issued: bool | None = None
    contact_info_provided: bool | None = None
    stated_cause: str | None = Field(default=None, max_length=300)
    alternate: AlternateFlight | None = None
    ticket_fare_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    # Separate statutory calculator inputs. Never infer these from total ticket fare.
    one_way_basic_fare_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    airline_fuel_charge_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    sector: Literal["domestic", "international"] | None = None
    scheduled_departure_local: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    distance_km: int | None = Field(default=None, ge=0, le=100_000)
    ticket_cost_including_taxes_inr: float | None = Field(default=None, ge=0, le=1_000_000)
    statement: str = Field(min_length=10, max_length=4000, description="Passenger's own account")

    @field_validator("statement", "stated_cause")
    @classmethod
    def reject_hidden_characters(cls, value: str | None) -> str | None:
        return _reject_control_or_zero_width(value)

    @field_validator("destination")
    @classmethod
    def route_must_differ(cls, value: str, info) -> str:
        if info.data.get("origin") == value:
            raise ValueError("origin and destination must differ")
        return value

    @field_validator("notice_minutes_before_departure")
    @classmethod
    def cancellation_requires_notice(cls, value: int | None, info) -> int | None:
        if info.data.get("disruption_type") == DisruptionType.cancellation and value is None:
            raise ValueError("notice_minutes_before_departure is required for cancellation")
        return value

    @field_validator("delay_minutes")
    @classmethod
    def delay_matches_disruption(cls, value: int | None, info) -> int | None:
        disruption = info.data.get("disruption_type")
        if disruption == DisruptionType.delay and value is None:
            raise ValueError("delay_minutes is required when disruption_type is 'delay'")
        if disruption in (DisruptionType.cancellation, DisruptionType.denied_boarding) and value:
            raise ValueError("delay_minutes does not apply to cancellation or denied_boarding")
        return value


class Route(StrEnum):
    """Stable route values; aliases preserve current API/worker member names."""

    PAY = "pay_as_computed"
    REQUEST_EVIDENCE = "request_further_evidence"
    DECLINE = "decline"
    ESCALATE = "escalate"

    pay_as_computed = PAY
    request_further_evidence = REQUEST_EVIDENCE
    decline = DECLINE
    escalate = ESCALATE


class EscalationReason(StrEnum):
    CAUSE_DISPUTED = "CAUSE_DISPUTED"
    PROVISION_CONFLICT = "PROVISION_CONFLICT"
    OUT_OF_CORPUS = "OUT_OF_CORPUS"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PROVIDER_DEGRADED = "PROVIDER_DEGRADED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    WORKER_FAILURE = "WORKER_FAILURE"

    # Backwards-compatible member aliases used by the current worker.
    disputed_cause = CAUSE_DISPUTED
    provision_conflict = PROVISION_CONFLICT
    out_of_corpus = OUT_OF_CORPUS
    verification_failed = VERIFICATION_FAILED
    budget_exhausted = BUDGET_EXCEEDED
    provider_degraded = PROVIDER_DEGRADED
    guardrail_triggered = GUARDRAIL_BLOCKED


class Chunk(BaseModel):
    """A normalized, traceable passage from one source document."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    chunk_id: str = Field(min_length=1)
    clause_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    heading_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    refs: list[str] = Field(default_factory=list)
    hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class Citation(BaseModel):
    """An exact quote supporting a recommendation; must resolve to a source chunk."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    doc_id: str
    quote: str
    chunk_id: str = Field(min_length=1)


class Result(BaseModel):
    """Stable result contract shared by the agent, API, and client."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    route: Route
    summary: str
    citations: list[Citation] = Field(default_factory=list)
    amount: int | None = Field(default=None, ge=0)  # whole rupees from compute_compensation
    # Non-monetary entitlements owed regardless of compensation (e.g. meals under CAR 3.8.1(a)).
    entitlements: list[str] = Field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    prompt_version: str
    corpus_version: str

    @model_validator(mode="after")
    def validate_route_requirements(self) -> Result:
        if self.route is Route.ESCALATE:
            if self.escalation_reason is None:
                raise ValueError("escalation_reason is required when route is ESCALATE")
        elif self.escalation_reason is not None:
            raise ValueError("escalation_reason is only allowed when route is ESCALATE")

        if self.route is Route.PAY:
            if self.amount is None:
                raise ValueError("amount is required when route is PAY")
            if not self.citations:
                raise ValueError("at least one citation is required when route is PAY")
        elif self.route is Route.DECLINE and not self.citations:
            raise ValueError("at least one citation is required when route is DECLINE")
        return self


class RunState(TypedDict, total=False):
    """Partial state shared by LangGraph nodes; each node fills its own fields."""

    claim: dict[str, object]
    facts: dict[str, object]
    retrieved: list[dict[str, object]]
    messages: list[dict]
    checklist: dict[str, dict]
    research_report: dict | None
    research_complete: bool
    compensation: dict | None
    draft: dict[str, object] | None
    verified: bool
    verified_draft: dict[str, object] | None
    verifier_outcomes: list[dict]
    attempts: int  # grounding-verifier retries used (max 2)
    awaiting_verification: bool  # split mode: draft stored, verifier-worker continues
    step_count: int
    started_at: float  # monotonic clock, local to the worker process
    budget_exceeded: bool
    tool_calls: int
    llm_calls: list[dict]  # per-call tokens/cost tagged with the node (#33)
    trace: list[dict[str, object]]
    result: dict[str, object]

    # The worker adapts Result to the legacy Recommendation API payload.
    run_id: str
    prompt_version: str
    corpus_version: str
    errors: list[str]


class Recommendation(BaseModel):
    route: Route
    summary: str
    entitlements: list[str] = Field(default_factory=list)
    compensation_inr: int | None = None  # whole rupees, computed by code, never by the LLM
    citations: list[Citation] = Field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    verified: bool = False
    requires_human_approval: Literal[True] = True


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    verifying = "verifying"  # draft handed to verifier-worker (#28); not terminal
    done = "done"
    completed = "completed"  # Legacy status value accepted during rollout.
    escalated = "escalated"
    failed = "failed"


TERMINAL_STATUSES = frozenset({
    RunStatus.done.value,
    RunStatus.completed.value,
    RunStatus.escalated.value,
    RunStatus.failed.value,
})


class RunOut(BaseModel):
    run_id: UUID
    status: RunStatus
    terminal: bool = False
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
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

"""Deterministic, clause-grounded compensation calculations for DGCA CAR Rev. 4.

The function computes arithmetic only. Eligibility facts that are not represented by
its inputs (for example, whether denial was against the passenger's will) must be
established before calling it. Monetary inputs are parsed through Decimal and final
fractional rupees are rounded half up to the nearest whole rupee.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CANCELLATION_TIERS = (
    (60, Decimal(5000), "3.3.2(a)"),
    (120, Decimal(7500), "3.3.2(b)"),
    (None, Decimal(10000), "3.3.2(c)"),
)
DENIED_BOARDING_ALTERNATE_TIERS = (
    (24 * 60, Decimal(2), Decimal(10000), "3.2.2(a)"),
    (None, Decimal(4), Decimal(20000), "3.2.2(b)"),
)
INTERNATIONAL_DOWNGRADE_TIERS = (
    (1500, Decimal("0.30")),
    (3500, Decimal("0.50")),
    (None, Decimal("0.75")),
)


@dataclass(frozen=True)
class CompensationResult:
    status: str
    amount: int | None
    formula: str
    clause_id: str
    inputs_used: dict[str, Any]
    missing_fields: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly structured result."""
        result = asdict(self)
        result["missing_fields"] = list(self.missing_fields)
        return result


def _needs_evidence(clause_id: str, *fields: str) -> CompensationResult:
    return CompensationResult(
        status="needs_evidence",
        amount=None,
        formula="not calculated; required evidence is missing",
        clause_id=clause_id,
        inputs_used={},
        missing_fields=tuple(fields),
    )


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite non-negative monetary value")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite non-negative monetary value") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be a finite non-negative monetary value")
    return amount


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _rounded(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _computed(
    *, amount: Decimal, formula: str, clause_id: str, inputs_used: dict[str, Any], reason: str | None = None
) -> CompensationResult:
    return CompensationResult(
        status="computed",
        amount=_rounded(amount),
        formula=formula,
        clause_id=clause_id,
        inputs_used=inputs_used,
        reason=reason,
    )


def _zero(clause_id: str, reason: str, inputs_used: dict[str, Any]) -> CompensationResult:
    return _computed(
        amount=Decimal(0),
        formula="INR 0; no compensation under the applicable rule",
        clause_id=clause_id,
        inputs_used=inputs_used,
        reason=reason,
    )


def _base_fare(inputs: dict[str, Any], required: list[str]) -> tuple[Decimal, dict[str, Any]] | CompensationResult:
    missing = [key for key in required if inputs.get(key) is None]
    if missing:
        return _needs_evidence("", *missing)
    basic = _money(inputs["one_way_basic_fare_inr"], "one_way_basic_fare_inr")
    fuel = _money(inputs["airline_fuel_charge_inr"], "airline_fuel_charge_inr")
    return basic + fuel, {
        "one_way_basic_fare_inr": str(basic),
        "airline_fuel_charge_inr": str(fuel),
    }


def _cancellation(inputs: dict[str, Any]) -> CompensationResult:
    clause = "3.3.2"
    notice = inputs.get("notice_minutes_before_departure")
    accepted = inputs.get("alternate_or_refund_accepted")

    if accepted is not None and not isinstance(accepted, bool):
        raise ValueError("alternate_or_refund_accepted must be a boolean")
    if accepted is True:
        return _zero(
            clause,
            "alternate accepted, 3.3.2",
            {"alternate_or_refund_accepted": True},
        )
    if notice is None:
        return _needs_evidence(clause, "notice_minutes_before_departure")
    notice = _integer(notice, "notice_minutes_before_departure")

    # CAR 3.3.1 requires an acceptable alternate or refund at both notice thresholds.
    # At exactly 24 h and exactly 2 weeks, the rule still applies; require evidence.
    if notice >= 24 * 60 and accepted is None:
        return _needs_evidence(clause, "alternate_or_refund_accepted")
    needed = [
        key
        for key in ("block_time_minutes", "one_way_basic_fare_inr", "airline_fuel_charge_inr")
        if inputs.get(key) is None
    ]
    if needed:
        return _needs_evidence(clause, *needed)

    block = _integer(inputs["block_time_minutes"], "block_time_minutes")
    fare_total_result = _base_fare(inputs, ["one_way_basic_fare_inr", "airline_fuel_charge_inr"])
    assert not isinstance(fare_total_result, CompensationResult)
    fare_total, fare_inputs = fare_total_result

    # CAR 3.3.2(a)-(c): inclusive upper band edges at 60 and 120 minutes.
    for upper, cap, tier_clause in CANCELLATION_TIERS:
        if upper is None or block <= upper:
            return _computed(
                amount=min(cap, fare_total),
                formula=f"min(INR {cap}, booked one-way basic fare + airline fuel charge)",
                clause_id=tier_clause,
                inputs_used={
                    "notice_minutes_before_departure": notice,
                    "alternate_or_refund_accepted": accepted,
                    "block_time_minutes": block,
                    **fare_inputs,
                },
            )
    raise AssertionError("unreachable cancellation tier")


def _denied_boarding(inputs: dict[str, Any]) -> CompensationResult:
    clause = "3.2.2"
    if inputs.get("alternate_arranged") is None:
        return _needs_evidence(clause, "alternate_arranged")
    alternate = _boolean(inputs["alternate_arranged"], "alternate_arranged")

    if alternate:
        delay = inputs.get("alternate_departure_delay_minutes")
        if delay is None:
            return _needs_evidence(clause, "alternate_departure_delay_minutes")
        delay = _integer(delay, "alternate_departure_delay_minutes")
        if delay <= 60:
            return _zero(
                clause,
                "alternate arranged to depart within one hour, 3.2.2",
                {"alternate_arranged": True, "alternate_departure_delay_minutes": delay},
            )

    missing = [
        key
        for key in ("one_way_basic_fare_inr", "airline_fuel_charge_inr")
        if inputs.get(key) is None
    ]
    if missing:
        return _needs_evidence(clause, *missing)
    fare_total_result = _base_fare(inputs, ["one_way_basic_fare_inr", "airline_fuel_charge_inr"])
    assert not isinstance(fare_total_result, CompensationResult)
    fare_total, fare_inputs = fare_total_result
    used: dict[str, Any] = {"alternate_arranged": alternate, **fare_inputs}

    if not alternate:
        subclause, multiplier, cap = "3.2.2(c)", Decimal(4), Decimal(20000)
        formula = "min(400% × (booked one-way basic fare + airline fuel charge), INR 20,000)"
    else:
        used["alternate_departure_delay_minutes"] = delay
        _, multiplier, cap, subclause = next(
            tier for tier in DENIED_BOARDING_ALTERNATE_TIERS if tier[0] is None or delay <= tier[0]
        )
        if subclause == "3.2.2(a)":
            formula = "min(200% × (booked one-way basic fare + airline fuel charge), INR 10,000)"
        else:
            formula = "min(400% × (booked one-way basic fare + airline fuel charge), INR 20,000)"

    return _computed(
        amount=min(multiplier * fare_total, cap),
        formula=formula,
        clause_id=subclause,
        inputs_used=used,
    )


def _downgrading(inputs: dict[str, Any]) -> CompensationResult:
    clause = "3.5.1"
    sector = inputs.get("sector")
    if sector is None:
        return _needs_evidence(clause, "sector")
    if sector not in {"domestic", "international"}:
        raise ValueError("sector must be 'domestic' or 'international'")
    fare = inputs.get("ticket_cost_including_taxes_inr")
    if fare is None:
        return _needs_evidence(clause, "ticket_cost_including_taxes_inr")
    fare_amount = _money(fare, "ticket_cost_including_taxes_inr")
    used: dict[str, Any] = {
        "sector": sector,
        "ticket_cost_including_taxes_inr": str(fare_amount),
    }

    if sector == "domestic":
        rate, tier_clause = Decimal("0.75"), "3.5.1(a)"
        formula = "75% × cost of ticket including taxes (domestic sector)"
    else:
        distance = inputs.get("distance_km")
        if distance is None:
            return _needs_evidence(clause, "distance_km")
        distance_amount = _money(distance, "distance_km")
        used["distance_km"] = str(distance_amount)
        # CAR 3.5.1(b): 1,500 km or less; >1,500 through 3,500 km; >3,500 km.
        _, rate = next(
            tier for tier in INTERNATIONAL_DOWNGRADE_TIERS if tier[0] is None or distance_amount <= tier[0]
        )
        tier_clause = "3.5.1(b)"
        if rate == Decimal("0.30"):
            formula = "30% × cost of ticket including taxes (international sector, distance ≤ 1,500 km)"
        elif rate == Decimal("0.50"):
            formula = "50% × cost of ticket including taxes (international sector, 1,500 km < distance ≤ 3,500 km)"
        else:
            formula = "75% × cost of ticket including taxes (international sector, distance > 3,500 km)"

    return _computed(
        amount=rate * fare_amount,
        formula=formula,
        clause_id=tier_clause,
        inputs_used=used,
    )


def compute_compensation(clause_id: str, inputs: dict[str, Any]) -> CompensationResult:
    """Compute a CAR compensation amount without filling missing facts by guesswork.

    ``clause_id`` accepts ``3.3.2`` (cancellation), ``3.2.2`` (denied
    boarding), or ``3.5``/``3.5.1`` (downgrading). The result's ``clause_id``
    identifies the specific subparagraph used where the CAR provides one.
    """
    if not isinstance(inputs, dict):
        raise TypeError("inputs must be a dictionary")
    calculators = {
        "3.3.2": _cancellation,
        "3.2.2": _denied_boarding,
        "3.5": _downgrading,
        "3.5.1": _downgrading,
    }
    try:
        calculator = calculators[clause_id]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported compensation clause: {clause_id!r}") from exc
    return calculator(inputs)

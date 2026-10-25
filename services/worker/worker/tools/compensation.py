"""Deterministic compensation arithmetic. The LLM interprets rules; code does the maths.

Amounts follow CAR Sec 3 Series M Part IV Rev 4, para 3.3.2 (cancellation, by block time).
The cancellation adapter uses the repository-pinned CAR text; full multi-disruption
calculation support is tracked separately in #15.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def cancellation_compensation_inr(block_time_minutes: int, one_way_basic_fare_plus_fuel_inr: float) -> float:
    return float(min(_cancellation_cap(block_time_minutes), one_way_basic_fare_plus_fuel_inr))


def _cancellation_cap(block_time_minutes: int) -> int:
    return 5000 if block_time_minutes <= 60 else 7500 if block_time_minutes <= 120 else 10000


def compute_compensation(clause_id: str, inputs: dict) -> dict:
    """Cancellation adapter until the full #15 calculator lands.

    Only explicit structured facts enter the arithmetic. A total ticket fare is
    never substituted for basic fare plus fuel. Other clauses need #15's tiers.
    """
    result = {"status": "needs_evidence", "amount": None, "formula": "not calculated",
              "clause_id": clause_id, "inputs_used": {}, "missing_fields": []}
    if clause_id != "3.3.2":
        return {**result, "status": "unsupported", "reason": "Full clause support is tracked in #15"}
    notice = inputs.get("notice_minutes_before_departure")
    accepted = inputs.get("alternate_flight_accepted")
    contact = inputs.get("contact_info_provided")
    if accepted is True:
        return {**result, "status": "computed", "amount": 0, "formula": "alternate accepted, 3.3.2",
                "inputs_used": {"alternate_flight_accepted": True}}
    if contact is False:
        return {**result, "status": "computed", "amount": 0, "formula": "no contact information, 3.3.3",
                "clause_id": "3.3.3", "inputs_used": {"contact_info_provided": False}}
    required = ["notice_minutes_before_departure", "alternate_flight_accepted", "contact_info_provided"]
    missing = [name for name in required if inputs.get(name) is None]
    if missing:
        return {**result, "missing_fields": missing}
    if not isinstance(notice, int) or isinstance(notice, bool) or notice < 0:
        raise ValueError("notice_minutes_before_departure must be a non-negative integer")
    if not isinstance(accepted, bool) or not isinstance(contact, bool):
        raise ValueError("alternate and contact facts must be booleans")
    # 3.3.1 needs an acceptable arrangement as well as timely notice.
    if notice >= 24 * 60:
        offered = inputs.get("alternate_or_refund_offered")
        if offered is None:
            return {**result, "missing_fields": ["alternate_or_refund_offered"]}
        if not isinstance(offered, bool):
            raise ValueError("alternate_or_refund_offered must be boolean")
        if offered:
            return {**result, "status": "computed", "amount": 0, "formula": "acceptable arrangement, 3.3.1",
                    "clause_id": "3.3.1", "inputs_used": {"notice_minutes_before_departure": notice,
                                                          "alternate_or_refund_offered": True}}
    required = ["block_time_minutes", "one_way_basic_fare_inr", "airline_fuel_charge_inr"]
    missing = [name for name in required if inputs.get(name) is None]
    if missing:
        return {**result, "missing_fields": missing}
    block = inputs["block_time_minutes"]
    if not isinstance(block, int) or isinstance(block, bool) or block < 0:
        raise ValueError("block_time_minutes must be a non-negative integer")
    # Kept behind this adapter so replacing it with #15 does not change the loop.
    try:
        if any(isinstance(inputs[name], bool) for name in required[1:]):
            raise ValueError("fare and fuel must be monetary values")
        money = [Decimal(str(inputs[name])) for name in required[1:]]
    except InvalidOperation as exc:
        raise ValueError("fare and fuel must be monetary values") from exc
    if any(not value.is_finite() or value < 0 for value in money):
        raise ValueError("fare and fuel must be finite non-negative amounts")
    cap = _cancellation_cap(block)
    amount = int(min(Decimal(cap), sum(money)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {**result, "status": "computed", "amount": amount,
            "formula": f"min(INR {cap}, booked one-way basic fare + airline fuel charge)",
            "clause_id": f"3.3.2({'a' if block <= 60 else 'b' if block <= 120 else 'c'})",
            "inputs_used": {name: value for name, value in inputs.items() if name in {*required,
                            "notice_minutes_before_departure", "alternate_flight_accepted",
                            "contact_info_provided", "alternate_or_refund_offered"}}}

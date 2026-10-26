from __future__ import annotations

import pytest
from worker.tools.compensation import compute_compensation


def cancellation_inputs(**overrides: object) -> dict[str, object]:
    return {
        "notice_minutes_before_departure": 23 * 60,
        "alternate_flight_accepted": False,
        "alternate_or_refund_offered": False,
        "block_time_minutes": 60,
        "one_way_basic_fare_inr": 4000,
        "airline_fuel_charge_inr": 1000,
        **overrides,
    }


def denied_boarding_inputs(**overrides: object) -> dict[str, object]:
    return {
        "alternate_arranged": True,
        "alternate_declined": False,
        "alternate_departure_delay_minutes": 61,
        "one_way_basic_fare_inr": 4000,
        "airline_fuel_charge_inr": 1000,
        **overrides,
    }


# CAR 3.3.2(a): "INR 5,000 or booked one-way basic fare plus airline fuel charge, whichever is less"
def test_cancellation_tier_a():
    result = compute_compensation(
        "3.3.2",
        cancellation_inputs(block_time_minutes=60, one_way_basic_fare_inr=7000, airline_fuel_charge_inr=1000),
    )
    assert (result.amount, result.clause_id) == (5000, "3.3.2(a)")


def test_cancellation_tier_a_fare_below_cap():
    result = compute_compensation(
        "3.3.2", cancellation_inputs(block_time_minutes=60, one_way_basic_fare_inr=3000, airline_fuel_charge_inr=0)
    )
    assert (result.amount, result.clause_id) == (3000, "3.3.2(a)")


# CAR 3.3.2(b): "INR 7,500 or booked one-way basic fare plus airline fuel charge, whichever is less"
def test_cancellation_tier_b():
    result = compute_compensation(
        "3.3.2", cancellation_inputs(block_time_minutes=61, one_way_basic_fare_inr=10_000, airline_fuel_charge_inr=0)
    )
    assert (result.amount, result.clause_id) == (7500, "3.3.2(b)")


def test_cancellation_tier_b_fare_below_cap():
    result = compute_compensation(
        "3.3.2", cancellation_inputs(block_time_minutes=61, one_way_basic_fare_inr=4000, airline_fuel_charge_inr=0)
    )
    assert (result.amount, result.clause_id) == (4000, "3.3.2(b)")


# CAR 3.3.2(c): "INR 10,000 or booked one-way basic fare plus airline fuel charge, whichever is less"
def test_cancellation_tier_c_applies_cap():
    result = compute_compensation(
        "3.3.2",
        cancellation_inputs(block_time_minutes=121, one_way_basic_fare_inr=12_000, airline_fuel_charge_inr=0),
    )
    assert (result.amount, result.clause_id) == (10_000, "3.3.2(c)")


# CAR 3.2.2(a): "200% of booked one-way basic fare plus airline fuel charge, subject to maximum of INR 10,000"
def test_denied_boarding_alternate_within_24_hours():
    result = compute_compensation("3.2.2", denied_boarding_inputs(alternate_departure_delay_minutes=1440))
    assert (result.amount, result.clause_id) == (10_000, "3.2.2(a)")


# CAR 3.2.2(b): "400% of booked one-way basic fare plus airline fuel charge, subject to maximum of INR 20,000"
def test_denied_boarding_alternate_after_24_hours():
    result = compute_compensation("3.2.2", denied_boarding_inputs(alternate_departure_delay_minutes=1441))
    assert (result.amount, result.clause_id) == (20_000, "3.2.2(b)")


# CAR 3.2.2(c): "refund of full value of ticket and compensation equal to 400% ... subject to maximum of INR 20,000"
def test_denied_boarding_passenger_declines_alternate():
    result = compute_compensation(
        "3.2.2", denied_boarding_inputs(alternate_declined=True, alternate_arranged=True)
    )
    assert (result.amount, result.clause_id) == (20_000, "3.2.2(c)")
    assert "refund" not in result.formula  # The amount field is compensation; ticket refund is separate.


def test_denied_boarding_declined_alternate_within_one_hour_is_zero():
    result = compute_compensation(
        "3.2.2", denied_boarding_inputs(alternate_declined=True, alternate_departure_delay_minutes=60)
    )
    assert (result.amount, result.clause_id) == (0, "3.2.2")


def test_denied_boarding_declined_alternate_missing_delay_needs_evidence():
    inputs = denied_boarding_inputs(alternate_declined=True)
    del inputs["alternate_departure_delay_minutes"]
    result = compute_compensation("3.2.2", inputs)
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("alternate_departure_delay_minutes",)


def test_denied_boarding_missing_passenger_choice_needs_evidence():
    inputs = denied_boarding_inputs()
    del inputs["alternate_declined"]
    result = compute_compensation("3.2.2", inputs)
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("alternate_declined",)


def test_denied_boarding_no_arrangement_without_decline_is_not_tier_c():
    result = compute_compensation(
        "3.2.2", denied_boarding_inputs(alternate_arranged=False, alternate_declined=False)
    )
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("alternate_declined",)


# CAR 3.5.1(a): "For domestic sector:-75% of the cost of ticket including taxes."
def test_domestic_downgrade_tier():
    result = compute_compensation(
        "3.5.1", {"sector": "domestic", "ticket_cost_including_taxes_inr": 10_000}
    )
    assert (result.amount, result.clause_id) == (7500, "3.5.1(a)")


# CAR 3.5.1(b): "30% ... for flights of 1500 km or less."
def test_international_downgrade_up_to_1500_km():
    result = compute_compensation(
        "3.5.1", {"sector": "international", "distance_km": 1500, "ticket_cost_including_taxes_inr": 10_000}
    )
    assert (result.amount, result.clause_id) == (3000, "3.5.1(b)")


# CAR 3.5.1(b): "50% ... for flights between 1500 km to 3500 km."
def test_international_downgrade_1501_through_3500_km():
    result = compute_compensation(
        "3.5.1", {"sector": "international", "distance_km": 3500, "ticket_cost_including_taxes_inr": 10_000}
    )
    assert (result.amount, result.clause_id) == (5000, "3.5.1(b)")


# CAR 3.5.1(b): "75% ... for flights more than 3500 km."
def test_international_downgrade_over_3500_km():
    result = compute_compensation(
        "3.5.1", {"sector": "international", "distance_km": 3501, "ticket_cost_including_taxes_inr": 10_000}
    )
    assert (result.amount, result.clause_id) == (7500, "3.5.1(b)")


def test_cancellation_band_edge_120_and_first_minute_after():
    at_edge = compute_compensation("3.3.2", cancellation_inputs(block_time_minutes=120))
    after_edge = compute_compensation("3.3.2", cancellation_inputs(block_time_minutes=121))
    assert (at_edge.clause_id, after_edge.clause_id) == ("3.3.2(b)", "3.3.2(c)")


def test_denied_boarding_one_hour_exception_boundary():
    within = compute_compensation("3.2.2", denied_boarding_inputs(alternate_departure_delay_minutes=60))
    after = compute_compensation("3.2.2", denied_boarding_inputs(alternate_departure_delay_minutes=61))
    assert (within.amount, within.reason) == (0, "alternate arranged to depart within one hour, 3.2.2")
    assert (after.amount, after.clause_id) == (10_000, "3.2.2(a)")


def test_notice_at_exactly_two_weeks_returns_zero_under_3_3_1():
    result = compute_compensation(
        "3.3.2",
        {
            "notice_minutes_before_departure": 14 * 24 * 60,
            "alternate_or_refund_offered": True,
            "refund_taken": True,
        },
    )
    assert (result.amount, result.reason, result.clause_id) == (
        0,
        "informed 2+ weeks ahead and alternate/refund arranged, 3.3.1",
        "3.3.1",
    )


def test_notice_at_exactly_two_weeks_missing_arrangement_needs_evidence():
    result = compute_compensation("3.3.2", {"notice_minutes_before_departure": 14 * 24 * 60})
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("alternate_or_refund_offered",)


def test_notice_at_exactly_two_weeks_without_arrangement_continues_to_3_3_2():
    result = compute_compensation(
        "3.3.2",
        cancellation_inputs(
            notice_minutes_before_departure=14 * 24 * 60,
            alternate_or_refund_offered=False,
        ),
    )
    assert (result.amount, result.clause_id) == (5000, "3.3.2(a)")


def test_notice_just_under_two_weeks_requires_offer_evidence():
    result = compute_compensation(
        "3.3.2",
        {"notice_minutes_before_departure": 14 * 24 * 60 - 1},
    )
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("alternate_or_refund_offered",)


def test_notice_at_exactly_24_hours_requires_3_3_1_offer():
    offered = compute_compensation(
        "3.3.2",
        cancellation_inputs(
            notice_minutes_before_departure=24 * 60,
            alternate_or_refund_offered=True,
            alternate_flight_accepted=None,
        ),
    )
    assert (offered.amount, offered.clause_id) == (0, "3.3.1")
    missing = compute_compensation(
        "3.3.2",
        cancellation_inputs(
            notice_minutes_before_departure=24 * 60,
            alternate_or_refund_offered=None,
        ),
    )
    assert missing.status == "needs_evidence"
    assert missing.missing_fields == ("alternate_or_refund_offered",)
    not_offered = compute_compensation(
        "3.3.2",
        cancellation_inputs(
            notice_minutes_before_departure=24 * 60,
            alternate_or_refund_offered=False,
        ),
    )
    assert (not_offered.amount, not_offered.clause_id) == (5000, "3.3.2(a)")


def test_notice_at_exactly_two_weeks_offered_returns_zero_under_3_3_1():
    result = compute_compensation(
        "3.3.2",
        cancellation_inputs(
            notice_minutes_before_departure=14 * 24 * 60,
            alternate_or_refund_offered=True,
        ),
    )
    assert (result.amount, result.clause_id) == (0, "3.3.1")


def test_notice_just_under_24_hours_uses_3_3_2_without_offer_fact():
    result = compute_compensation(
        "3.3.2",
        cancellation_inputs(notice_minutes_before_departure=24 * 60 - 1),
    )
    assert (result.amount, result.clause_id) == (5000, "3.3.2(a)")


@pytest.mark.parametrize(
    ("notice", "missing_field"),
    [
        (0, "alternate_flight_accepted"),
        (24 * 60 - 1, "alternate_flight_accepted"),
        (24 * 60, "alternate_or_refund_offered"),
        (14 * 24 * 60 - 1, "alternate_or_refund_offered"),
        (14 * 24 * 60, "alternate_or_refund_offered"),
    ],
)
def test_missing_notice_band_fact_never_computes_a_payout(notice: int, missing_field: str):
    inputs = cancellation_inputs(notice_minutes_before_departure=notice)
    inputs[missing_field] = None
    result = compute_compensation("3.3.2", inputs)
    assert result.status == "needs_evidence"
    assert result.amount is None
    assert result.missing_fields == (missing_field,)


def test_refund_taken_does_not_suppress_cancellation_compensation():
    result = compute_compensation(
        "3.3.2", cancellation_inputs(alternate_flight_accepted=False, refund_taken=True)
    )
    assert (result.amount, result.clause_id) == (5000, "3.3.2(a)")
    assert result.inputs_used["refund_taken"] is True


def test_conflicting_accepted_alternate_and_refund_facts_are_rejected():
    with pytest.raises(ValueError, match="cannot both be true"):
        compute_compensation(
            "3.3.2", cancellation_inputs(alternate_flight_accepted=True, refund_taken=True)
        )


def test_alternate_accepted_demo_returns_zero_with_requested_reason():
    result = compute_compensation("3.3.2", {"alternate_flight_accepted": True})
    assert (result.amount, result.reason, result.clause_id) == (0, "alternate accepted, 3.3.2", "3.3.2")


def test_missing_block_time_returns_needs_evidence_without_guessing():
    inputs = cancellation_inputs()
    del inputs["block_time_minutes"]
    result = compute_compensation("3.3.2", inputs)
    assert result.status == "needs_evidence"
    assert result.missing_fields == ("block_time_minutes",)


def test_missing_denied_boarding_alternate_delay_returns_needs_evidence():
    inputs = denied_boarding_inputs()
    del inputs["alternate_departure_delay_minutes"]
    result = compute_compensation("3.2.2", inputs)
    assert result.missing_fields == ("alternate_departure_delay_minutes",)


def test_result_has_required_structured_fields_and_json_friendly_values():
    result = compute_compensation("3.3.2", cancellation_inputs())
    payload = result.to_dict()
    assert payload["amount"] == 5000
    assert payload["clause_id"] == "3.3.2(a)"
    assert "formula" in payload and "inputs_used" in payload
    assert isinstance(payload["missing_fields"], list)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "not money"])
def test_invalid_fares_are_rejected(value: object):
    with pytest.raises(ValueError):
        compute_compensation("3.3.2", cancellation_inputs(one_way_basic_fare_inr=value))


def test_fractional_rupees_are_rounded_half_up():
    result = compute_compensation(
        "3.5.1", {"sector": "domestic", "ticket_cost_including_taxes_inr": "2.00"}
    )
    assert result.amount == 2

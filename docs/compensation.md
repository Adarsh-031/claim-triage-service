# DGCA compensation arithmetic (CAR Rev. 4)

`worker.tools.compensation.compute_compensation(clause_id, inputs)` applies the
published arithmetic deterministically. It does not decide facts such as whether a
passenger reported on time, denial was involuntary, or a cancellation was caused by
an extraordinary circumstance; those facts must be resolved by the triage workflow.
The calculator returns an integer-rupee `amount`, the exact `formula`, the most
specific applicable `clause_id`, and normalized `inputs_used`. Missing facts produce
`status: "needs_evidence"` and `missing_fields`, without an estimated amount.
Fractional rupees are rounded half up to the nearest whole rupee.

The figures below are transcribed from the repository's CAR Section 3, Series M,
Part IV, Revision 4 text in `tests/fixtures/car-s3-m-iv-r4.layout.txt`.

## Workflow integration status

This module is a deterministic calculation component, not yet a registered or invoked
LangGraph tool. The current graph uses demo corpus fixtures and routes claims to human
review because grounding verification is not implemented; its outputs must not be
presented as a completed claim determination.

The current `ClaimIn`/web form captures `ticket_fare_inr` and a nested `alternate`
object. It does not capture the separate one-way basic fare and fuel charge needed by
3.3.2/3.2.2, `refund_taken`, or downgrade sector, distance, and ticket cost including
taxes. An adapter must map `alternate.accepted` to `alternate_flight_accepted` and
collect those missing values explicitly. Do not derive basic fare/fuel from the total
ticket fare, since it also may include taxes and fees. Until these facts are collected,
the calculator must return `needs_evidence` for the missing fields.

## Cancellation — CAR 3.3.2

Compensation under 3.3.2 applies when the passenger was not informed as required by
3.3.1. The notice bands are handled explicitly:

- At least 14 days (20,160 minutes): return zero with reason
  `informed 2+ weeks ahead, 3.3.1`.
- Less than 14 days and at least 24 hours (1,440 minutes): require
  `alternate_or_refund_offered`; if true, return zero under 3.3.1. If false, continue
  to 3.3.2, still requiring `alternate_flight_accepted` to be known.
- Less than 24 hours: use 3.3.2 directly, but require
  `alternate_flight_accepted`; a missing value returns `needs_evidence`.

`refund_taken` is separate and does not reduce or suppress a 3.3.2 tier. Only an
accepted alternate returns zero with reason `alternate accepted, 3.3.2`. The full
refund is a separate entitlement; this function returns the compensation amount only.

| Block time | Compensation | Clause |
| --- | --- | --- |
| Up to and including 1 hour (60 minutes) | Lesser of INR 5,000 or booked one-way basic fare plus airline fuel charge | 3.3.2(a) |
| More than 1 hour and up to and including 2 hours (120 minutes) | Lesser of INR 7,500 or booked one-way basic fare plus airline fuel charge | 3.3.2(b) |
| More than 2 hours | Lesser of INR 10,000 or booked one-way basic fare plus airline fuel charge | 3.3.2(c) |

The full ticket refund mentioned in 3.3.2 is a separate entitlement; this function's
`amount` is the compensation amount only. Same-ticket missed connections are a second
3.3.2 trigger but are currently out of scope because the claim contract lacks the
original and connecting flight facts.

## Denied boarding — CAR 3.2.2

| Alternate arranged | Compensation | Clause |
| --- | --- | --- |
| Scheduled to depart within 1 hour (60 minutes) of original departure | No compensation | 3.2.2 |
| More than 1 hour and within 24 hours (up to and including 1,440 minutes) | Lesser of 200% of (booked one-way basic fare + airline fuel charge) or INR 10,000 | 3.2.2(a) |
| More than 24 hours (over 1,440 minutes) | Lesser of 400% of (booked one-way basic fare + airline fuel charge) or INR 20,000 | 3.2.2(b) |
| Passenger declines the alternate flight (`alternate_declined=true`) | Lesser of 400% of (booked one-way basic fare + airline fuel charge) or INR 20,000, plus full ticket refund | 3.2.2(c) |

Clause (c) is selected from the passenger's explicit choice, not merely because the
airline did not arrange an alternate. If there is no arrangement and no explicit
passenger choice, the calculator requests evidence. The ticket refund in (c) is not
included in `amount`.

## Downgrading — CAR 3.5.1

The base is the cost of the ticket **including taxes**. Domestic/international status
and (for international travel) distance must be supplied.

| Sector / distance | Reimbursement | Clause |
| --- | --- | --- |
| Domestic | 75% of ticket cost including taxes | 3.5.1(a) |
| International, 1,500 km or less | 30% of ticket cost including taxes | 3.5.1(b) |
| International, more than 1,500 km through 3,500 km | 50% of ticket cost including taxes | 3.5.1(b) |
| International, more than 3,500 km | 75% of ticket cost including taxes | 3.5.1(b) |

The notice thresholds are tested at exactly 14 days (20,160 minutes) and 24 hours
(1,440 minutes). The international distance bands are implemented without overlap: exactly 1,500 km is
in the 30% band, and exactly 3,500 km is in the 50% band.

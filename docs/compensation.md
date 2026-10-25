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

## Cancellation — CAR 3.3.2

Compensation is available only when the passenger was not informed in accordance
with 3.3.1. The 3.3.1 arrangements depend on notice: at least two weeks, the airline
must inform the passenger and arrange an acceptable alternate flight/refund; under
two weeks and up to 24 hours, it must offer an alternate or refund acceptable to the
passenger. The calculator requires `alternate_flight_accepted` at every notice level;
a missing value returns `needs_evidence`. `refund_taken` is a separate input and does
not reduce or suppress the compensation amount: a passenger who takes the refund
remains eligible for the 3.3.2 compensation tier. Only an accepted alternate returns
zero, with reason `alternate accepted, 3.3.2`. Notice is also required to compute a
tier, and missing facts are never inferred from notice time alone.

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
| Passenger does not opt for an alternate flight | Lesser of 400% of (booked one-way basic fare + airline fuel charge) or INR 20,000, plus full ticket refund | 3.2.2(c) |

When no alternate is arranged, `alternate_arranged` must be `false`; no departure
interval is then needed. The ticket refund in (c) is not included in `amount`.

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

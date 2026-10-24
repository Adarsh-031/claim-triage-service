# Evaluation set (L3)

The fixed scenario set currently contains 24 JSON cases (`s01`–`s24`). Each holds `claim`,
`expected.route`, and the clause IDs an answer must rest on. Compensation amounts are deliberately
left out until the expected answers have been independently reviewed.

| Area | Examples |
|---|---|
| Cancellation | Demo alternate; notice under/over two weeks; late notice; missing contact; extraordinary circumstances |
| Delay | Two-hour short/long block-time thresholds; over-six-hour remedy; overnight and >24-hour hotel facilities |
| Denied boarding | Alternate within one hour; alternate within 24 hours; no alternate; signed/unsigned voucher agreement |
| Downgrade | Domestic involuntary downgrade reimbursement |
| Escalation | Technical fault vs. IndiGo force-majeure wording; Vietnam route outside corpus |
| Evidence and safety | Missing booking contact; prompt injection; malformed PNR and forbidden extra field |

`tests/test_scenarios.py` checks every fixture against `ClaimIn`, expects invalid fixtures to fail
validation, and verifies scenario IDs, expected-route metadata, and escalation reasons.

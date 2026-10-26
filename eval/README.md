# Evaluation set (L3)

The fixed scenario set currently contains 25 JSON cases (`s01`–`s25`). Each holds `claim`,
`expected.route`, and the clause IDs an answer must rest on. Compensation amounts are deliberately
left out until the expected answers have been independently reviewed.

| Area | Examples |
|---|---|
| Cancellation | Demo alternate; notice under/over two weeks; late notice; missing contact; extraordinary circumstances |
| Delay | Two-hour short/long block-time thresholds; exact and over-six-hour remedy boundary; overnight and >24-hour hotel facilities |
| Denied boarding | Alternate within one hour; alternate within 24 hours; no alternate; signed/unsigned voucher agreement |
| Downgrade | Domestic involuntary downgrade reimbursement |
| Escalation | Technical-fault CAR/CoC provision conflict; Vietnam route outside corpus |
| Evidence and safety | Missing booking contact; prompt injection; malformed PNR and forbidden extra field |

`tests/test_scenarios.py` checks every fixture against `ClaimIn`, expects invalid fixtures to fail
validation, and verifies scenario IDs, expected-route metadata, and escalation reasons.

## Running the live evaluation

Start the API and worker, then run both prompt versions with:

```sh
make eval
```

Select a single version with `make eval PROMPT=v1` or `make eval PROMPT=v2`. The CLI
submits each scenario with `X-Prompt-Version`, polls its run to completion, and writes
`eval/results/eval-results.json` plus `eval/results/eval-report.md`. Both artifacts are
generated outputs and are ignored by Git. Override `--api`, `--scenarios`,
`--output-dir`, `--timeout`, or `--poll-interval` when invoking `uv run python
cli/evaluate.py` directly.

The report computes route accuracy, citation resolution against indexed source chunks
(or presence of required citation fields if the corpus is not indexed), and recall of
each scenario's `must_cite` clause list. It records exact run payloads and the
`manifest.yaml` hash for audit. Semantic
feedback metrics (groundedness, context relevance, answer relevance, hallucination
rate) are explicitly `null` until a feedback provider is configured. On the current
deterministic skeleton, v1/v2 are run metadata and their measured outputs may be
identical; they are not evidence of a prompt-quality improvement.

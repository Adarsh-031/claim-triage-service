# Evaluation report

Status: live semantic comparison not yet measured.

PR #42 provides an executable v1/v2 comparison with TruLens feedback and a separate
CI integration run. No live judge credentials are configured for this workspace,
and the local Docker Linux engine is unavailable. The table below deliberately
contains no invented scores. `make eval` replaces this file with measured results.

| Prompt | Route accuracy | Citation accuracy | Groundedness | Context relevance | Answer relevance | Hallucination rate |
| --- | --- | --- | --- | --- | --- | --- |
| v1 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |
| v2 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |

The deliberate v2 change requests rule-condition checks before a recommendation.
Both versions execute their instructions in the research provider while preserving
the deterministic decision and verification gates. Hallucination rate is defined
as the 1 − groundedness judge-score proxy, not a factual-error count.

The `evaluation-smoke` GitHub Actions artifact runs all 28 scenarios through real
PostgreSQL, Redis, API and worker processes with a deterministic HTTP provider stub.
It checks workflow execution and reports real route mismatches; it does not measure
model quality. See [evaluation instructions](../eval/README.md) for configuration,
calculator gold cases, and the command that produces the measured report. Calculator
amount accuracy is sourced from the private worker trace; the API intentionally
withholds unverified amounts from its final recommendation.

## Measured CI smoke baseline (2026-10-26)

Source: [GitHub Actions run 40370989968](https://github.com/Adarsh-031/claim-triage-service/actions/runs/40370989968),
artifact `evaluation-smoke`. This uses a deterministic HTTP model stub and committed
corpus fixtures with real PostgreSQL, Redis, API, and worker processes.

| Prompt | Scenarios | Correct routes | Route accuracy | Citation accuracy | Required-clause recall | Execution errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 | 28 | 6 | 21.43% | 100% | 53.57% | 0 |
| v2 | 28 | 6 | 21.43% | 100% | 53.57% | 0 |

Citation accuracy averages scenarios with returned citations; missing citations are
also reflected in required-clause recall. This is **not** a 100% success rate for
claims. Expected routes are deliberately preserved so that unfinished verification
and decision handling stay visible. Identical stub outputs do not establish that
one prompt is better. TruLens scores and payable amount accuracy were not measured
in this smoke run.

Corpus chunks SHA-256: `5fdbd5876c07a2eea4b86d68491fcc9527313b5fa9f25774019fbed023fd6b9a`.


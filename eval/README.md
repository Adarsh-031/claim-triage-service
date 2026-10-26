# Evaluation set (L3)

The 28 committed scenarios exercise cancellation, delay, denied boarding, downgrade,
quarantine, missing evidence, and escalation. Their expected routes are acceptance
criteria, not values rewritten to match today's agent. The independent grounding
verifier remains incomplete, so safe escalation can legitimately fail a desired
payment-route expectation.

## Run a measured comparison

1. Start the API and worker from this branch and ingest the corpus. Use the same
   processed corpus locally and in the worker; the CLI rejects a corpus-version mismatch.
2. Configure the research provider/model in `.env`. Export `EVAL_JUDGE_MODEL`,
   `EVAL_JUDGE_API_KEY`, and optionally `EVAL_JUDGE_BASE_URL` in the CLI's environment.
   These judge variables are separate from the worker provider. The CLI does not
   load `.env` automatically. Never commit actual keys.
3. Run `make eval`. This installs the locked `evaluation` dependency group and runs
   every scenario with both prompts. Use `make eval PROMPT=v1` or `PROMPT=v2` for one.

The command writes `docs/eval-report.md`, raw run payloads and traces to
`eval/results/eval-results.json`, and a copy of the Markdown report to that directory.
Override paths and timing with `EVAL_ARGS`, for example:

```sh
make eval EVAL_ARGS='--api http://localhost:8000 --corpus-dir corpus/processed --timeout 180'
```

For plumbing checks without a judge, use `make eval EVAL_ARGS='--semantic off'`.
Semantic scores then remain N/A. A stub-provider run is not evidence of prompt-quality
improvement. CI runs this mode through real PostgreSQL, Redis, API, worker, and HTTP
provider stub, and uploads the reports and service logs as `evaluation-smoke`.

## What is measured

- The worker appends `prompts/v1.md` or `v2.md` to its shared research system prompt.
  The deliberate v2 change asks for rule-condition checks before the recommendation.
  Both retain the same deterministic decision and independent-verification gates.
  The intake trace records the executed version, which the CLI checks.
- Citation accuracy requires the local chunk, document, clause, and exact nonempty
  quote to match. Missing source data cannot score as a resolved citation.
- Required-clause recall counts only citations that resolve to actual source chunks.
- TruLens `OpenAI` feedback measures groundedness, context relevance across retrieved
  passages, and answer relevance. Context is recovered from tool trace chunk IDs,
  not just the passages the final answer chose to cite. Only allowlisted structured
  claim facts are sent as the question, never PNR or passenger narrative.
- Hallucination rate is the **1 − groundedness judge-score proxy**. It is not an
  independently verified factual-error frequency. Each metric reports its measured
  scenario count; missing context and judge failures remain explicit.
- Raw artifacts preserve run IDs, terminal payloads, traces, prompt hashes, corpus
  hash, and provider model identity for scored cases. Worker failures and wrong
  executed prompt/corpus versions are errors, not correct escalations.
- `expected.compensation_inr`, when present, is compared with the deterministic
  compensation result in the worker's internal trace and reported as calculator
  amount accuracy separately from route accuracy. Final API amounts remain withheld
  until independent grounding verification exists.

## Outstanding acceptance evidence

A live model/judge comparison must still be run and reviewed before claiming a
prompt-quality improvement. CI smoke reports validate the workflow only.

The API accepts explicit basic fare, fuel charge, and downgrade inputs. Do not invent
payable scenario amounts from total ticket fare or change gold routes to force a pass.
Only add reviewed `expected.compensation_inr` amounts to scenarios whose eligibility
facts and separate monetary inputs support a deterministic calculator result.

TruLens API reference: https://www.trulens.org/reference/trulens/providers/openai/provider/


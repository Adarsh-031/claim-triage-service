# Grounding verifier (issue #27)

The verifier checks the research agent's draft before anything is published. It never
writes a decision of its own: it passes the draft unchanged, or reports what failed so
the graph can retry retrieval or escalate. Code: `services/worker/worker/agent/verifier.py`,
prompt: `prompts/verifier_v1.md`.

## Checks

Deterministic checks run first. If any fails, the LLM is not called.

| Check | What it rejects |
|---|---|
| `citations_retrieved` | a citation whose chunk was not retrieved in this run, or whose clause/doc does not match it; a CAR clause named in the summary that is not cited |
| `quotes_grounded` | a quote that is not a substring of the chunk text in `chunks.jsonl` (whitespace is normalised, nothing else) |
| `checklist_complete` | a checklist item with missing legal sources, or unfinished research behind a pay/decline |
| `amount_matches` | a pay/decline amount that differs from a fresh `compute_compensation` run on the trusted claim facts; any amount on request-evidence |
| `route_allowed` | any route other than pay, decline or request evidence; an uncited pay/decline; an escalation reason on a non-escalation route |
| `no_payment_authorisation` | wording such as "payment approved", "has been paid", "will be paid" |

Then one LLM call (`llm_support`): each summary sentence is paired with the quotes of the
CAR clauses it names, and the model answers `supported: true/false` with a reason for each.
For IndiGo claims the same call compares CAR 1.4/1.5 with CoC 1.1/14.2 for the claim's
facts. A malformed or incomplete answer fails the check.

Example (s01): "Meals and refreshments are owed in relation to waiting time (CAR 3.3.2,
3.8.1(a))." is sent with the exact 3.3.2 and 3.8.1(a) text; both must come back supported.

## Outcomes

- All checks pass → `verified=True`; finalize publishes that exact draft.
- The CAR/CoC comparison finds lower protection → escalate `PROVISION_CONFLICT` (no retry).
- Verifier provider unavailable → escalate `PROVIDER_DEGRADED` (no retry).
- Anything else fails → retry 1 follows `refs.json` cross-references out of every retrieved
  clause (and fetches missing checklist clauses), retry 2 runs BM25 searches for the missing
  clauses; each retry re-drafts and re-verifies. After 2 retries → `VERIFICATION_FAILED`.

A failed draft is never returned: the escalation keeps only citations whose quotes are in the
source text. Retries are deterministic retrieval, not another research-LLM loop, and do not
use the model step/tool budget; the wall-clock run budget still applies.

## Stored outcomes

Every verify step is in the run trace (`step: verify`, with `attempt`, `passed`, `failed`,
`llm_called`, `lower_protection`). The worker also writes one `verifier_outcomes` row per
attempt after the run is persisted; a storage error is logged and never fails the run.

## Stub

`services/llm-stub` recognises the verifier prompt and answers `supported` for every
non-empty quote; it reports lower protection only for a technical/mechanical stated cause.
Tests use a fake LLM to cover the unsupported, malformed and conflict paths.

## Separate verifier-worker (#28)

In Compose the verifier runs in its own container (`verifier-worker`, same image,
`python -m worker.verifier_main`). Both workers set `VERIFIER_MODE=split`; `inline`
(the default for unit tests and `cli/evaluate --in-process`) keeps the whole graph in one process.

Hand-off:

1. agent-worker runs intake -> research -> draft, then a `handoff` step. In one transaction it
   stores a `drafts` row (key: `run_id` + run claim attempt; JSON state without the research chat,
   with the remaining wall-clock budget as `deadline_epoch`) and moves the run `running -> verifying`.
   It then pushes `{run_id, run_attempt, trace_context}` to `triage:verify`.
2. verifier-worker `BLMOVE`s the message into `triage:verify:processing`, claims the draft
   (`pending -> verifying`, `claim_count + 1`), heartbeats every 10 s, and runs one verify attempt.
3. Pass, conflict or provider outage: finalize and store the result (fenced by draft claim and run
   attempt). Grounding failure: draft -> `retry`, run -> `queued` (`resume_count + 1`), and a
   `{resume: true}` job goes back to `triage:runs`; an agent-worker expands retrieval (retry 1
   cross-references, retry 2 BM25), re-drafts and hands off again. After 2 retries -> `VERIFICATION_FAILED`.

Never lost:

- verifier killed mid-run: its heartbeat stops; the reaper (lease-guarded, every 15 s) puts the draft
  back to `pending` and the message back on `triage:verify`. After 3 claims the run escalates.
- verifier down / message lost: a draft still `pending` after `VERIFY_WAIT_S` (120 s) escalates
  (both workers run this sweep).
- verifier never comes back: the agent-worker's reaper also covers the verify queue, so the claimed
  draft is reclaimed and then escalates after `VERIFY_WAIT_S`.
- stale verifier: every write is compare-and-set on `claim_count`, so a reclaimed verifier cannot publish.
- verifier resumes are counted in `resume_count`, not against the crash-recovery limit.

Each trace has `host` on the `handoff` step (agent-worker) and on every `verify` step
(verifier-worker); Jaeger shows the `triage-worker` and `triage-verifier` services in one trace.
Existing volumes: apply `deploy/postgres/migrations/20261101_verifier_worker.sql`.

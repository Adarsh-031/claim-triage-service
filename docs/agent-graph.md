# Minimal agent graph (issue #6)

The worker runs `intake → research → verify → finalize`. Intake copies the structured
disruption type. Research makes one search against three hand-written in-memory chunks,
then uses a deterministic in-process drafting stub. Verification passes the draft through
and leaves `verified=False`; a separate grounding verifier is future work.

Scenario s01 returns `ESCALATE` with `VERIFICATION_FAILED` and fixture citations to
`3.3.1`, `3.3.2`, and `3.8.1(a)`. No claim determination or entitlement is asserted.
Claims with no retrieved fixture evidence escalate with `OUT_OF_CORPUS`. Finalization
also replaces any non-escalation draft with `VERIFICATION_FAILED` until grounding
verification is implemented, so a substituted draft cannot publish a decline or payment
through the worker/API.

Fixture chunk/document IDs start with `DEMO-CAR`, and `corpus_version` is `demo-fixture-v1`.
The texts are hand-written summaries, not authoritative source quotes. Each citation quotes
the actual retrieved fixture text and resolves to its fixture chunk. This keeps the skeleton
independent of ingestion, Chroma, network access, provider credentials, and the HTTP LLM stub.
The existing provider interface remains available for the future reasoning implementation.

## Budgets

Defaults are 12 work steps, 15 retrieval-tool calls, and 60 elapsed seconds. The graph
checks for exhaustion before and after each work node and before finalization. Reaching
any threshold produces an `ESCALATE` result with `BUDGET_EXCEEDED`, taking precedence over
a draft. Work steps and tool calls reserve a counter slot before executing. If that
reservation reaches the threshold, execution is skipped and the run finalizes. For example,
a tool counter of 14 reserves slot 15 and escalates without calling search; an existing
counter of 15 also skips search without incrementing again. With a threshold of 15, at
most 14 searches execute. This intentionally treats the threshold as an exhaustion boundary.
A search that exhausts the time budget skips drafting. A research node that returns no
draft loops until a budget is exhausted.

`step_count` counts reservations for intake, research, and verify, including retries and
the terminal reservation that skips execution.
Finalization is outside the work-step budget so it can always construct a valid result.
`tool_calls` counts search reservations (including the terminal skipped call), not drafting. Elapsed time uses a monotonic clock, unaffected
by system clock adjustments. These are cooperative checks between operations; they do
not interrupt a blocked tool call. Future external tools/providers must also enforce their
own call timeouts.

The LangGraph recursion limit is `agent_max_steps + 2`: enough for all allowed work nodes,
finalization, and the graph termination check. Using 12 for both the work budget and
recursion limit can raise a recursion exception before the final result is constructed.

## Running and validation

```bash
uv run pytest -q tests/test_graph.py
uv run ruff check .
uv run pytest -q
```

`run_agent(claim, correlation_id, run_id=...)` returns `(Result, trace)`. The queue worker
supplies the persisted run ID and converts `Result` to the legacy `Recommendation` used by
the API/store. It keeps human approval required and verification false. This avoids changing
the public response format as part of a graph skeleton issue.

Tests cover s01 and citation resolution, exact step/tool limits with a looping fake node,
the 60-second deadline using a fake clock, exhausted entry budgets, unsupported claims,
and the worker's escalation persistence path. Regression tests spy on search to prove
it is skipped at the reservation boundary and inject consequential drafts to prove they
cannot reach the persisted API payload.

# Bounded agent graph and retrieval tools (issue #23)

The worker runs `intake → research → verify → finalize`. Intake copies the structured
disruption type. Research uses the LangChain `search_corpus` tool against the configured
Chroma collection and performs a second targeted retrieval for meals and refreshments, then
deduplicates retrieved evidence by `chunk_id`. Local `mode="bm25"` searches the generated
`corpus/processed/chunks.jsonl` file. The deterministic draft remains a safety stub.
Verification is deliberately pass-through and leaves `verified=False`; a separate grounding
verifier is future work.

Scenario s01 is a smoke scenario for the real corpus. Run it after `docker compose up` using
`docker compose run --rm worker python scripts/smoke_s01.py`. It prints the route, escalation
reason, corpus version and retrieved citation clauses, and exits non-zero if clauses
`3.3.1`, `3.3.2`, and `3.8.1(a)` are not present. The expected s01 result remains
`ESCALATE` with `VERIFICATION_FAILED`; no claim determination or entitlement is asserted.
The smoke command uses the same configured Chroma collection and source chunks as the worker.

The graph keeps `verified=False` and never makes a claim determination. Finalization replaces
any non-escalation draft with `VERIFICATION_FAILED` until grounding verification is implemented,
so a substituted draft cannot publish a decline or payment through the worker/API. Unsupported
searches with no result above the vector similarity threshold escalate with `OUT_OF_CORPUS`.

## Retrieval and corpus integrity

`search_corpus(query, k=5, doc_id=None, mode="vector")` returns a dictionary with `chunks`
and `retrieval_error`. Supported document IDs are `CAR-S3-M-IV-R4` (DGCA regulatory floor),
`INDIGO-COC-DOM` (domestic carriage), and `INDIGO-COC-INTL` (international carriage).
Vector matches below the minimum cosine-similarity threshold are discarded. Vector candidates are reranked with reciprocal-rank fusion (RRF) over BM25 rankings so broad parent sections do not crowd out relevant clause-level evidence. Ingestion reports this reranked shortlist separately from BM25 top-three results. Retrieved passage
text is accepted only when its SHA-256 hash matches the indexed metadata. `get_clause` looks up
the exact normalized chunk from `chunks.jsonl` and returns a copy so callers cannot mutate the
shared cache.

The worker lazily caches the Chroma client, collection and embedding function behind a lock.
Compose shares the `embedding_cache` volume populated by ingestion. Local BM25 token counts and
document frequencies are cached per corpus file version. Retrieval failures return structured
errors and are not raised into the graph; unexpected programming errors are logged with a
traceback and marked `internal_error`.

## Budgets

Defaults are 12 work steps, 15 retrieval-tool calls, and 60 elapsed seconds. The graph checks
for exhaustion before and after each work node and before finalization. Reaching any threshold
produces an `ESCALATE` result with `BUDGET_EXCEEDED`, taking precedence over a draft. Work steps
and tool calls reserve a counter slot before executing. If that reservation reaches the
threshold, execution is skipped and the run finalizes. For example, a tool counter of 14
reserves slot 15 and escalates without calling search; an existing counter of 15 also skips
search without incrementing again. With a threshold of 15, at most 14 searches execute.
A search that exhausts the time budget skips drafting. A research node that returns no draft
loops until a budget is exhausted.

`step_count` counts reservations for intake, research, and verify, including retries and the
terminal reservation that skips execution. Finalization is outside the work-step budget so it
can always construct a valid result. `tool_calls` counts search reservations, including a
terminal skipped call, not drafting. Elapsed time uses a monotonic clock, unaffected by system
clock adjustments. These are cooperative checks between operations; they do not interrupt a
blocked tool call.

The retrieval timeout returns control to the graph within `TOOL_TIMEOUT_S`, but Python cannot
forcibly cancel an already blocked HTTP call. The implementation uses one daemon thread and a
single-flight gate so repeated timeouts do not accumulate unbounded threads; calls fail fast
while a previous operation remains blocked. The underlying HTTP operation may continue until
its client returns. Future external tools/providers must enforce their own request timeouts.

The LangGraph recursion limit is `agent_max_steps + 2`: enough for all allowed work nodes,
finalization, and the graph termination check. Using 12 for both the work budget and recursion
limit can raise a recursion exception before the final result is constructed.

## Running and validation

```bash
uv run pytest -q tests/test_corpus_tools.py tests/test_graph.py tests/test_index_corpus.py
uv run ruff check .
uv run pytest -q
docker compose up --build -d
docker compose run --rm worker python scripts/smoke_s01.py
```

`run_agent(claim, correlation_id, run_id=...)` returns `(Result, trace)`. The queue worker
supplies the persisted run ID and converts `Result` to the legacy `Recommendation` used by
the API/store. It keeps human approval required and verification false. This avoids changing
the public response format as part of the retrieval issue.

Tests cover the s01 graph path with injected results, exact step/tool limits, the deadline with
a fake clock, unsupported claims, worker escalation persistence, vector/BM25 ranking and
document filtering, exact clause lookup, source-hash integrity, timeout handling, and collection
verification. Real-corpus smoke checks run from `cli/index_corpus.py --verify` during Compose
ingestion and from the worker's `smoke_s01.py` command.

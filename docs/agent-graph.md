# Minimal agent graph (issue #6)

The worker runs `intake → research → verify → finalize`. Intake copies the structured
disruption type. Research calls the LangChain `search_corpus` tool against the configured
Chroma collection; local `mode="bm25"` searches the generated
`corpus/processed/chunks.jsonl` file. The deterministic draft remains a safety stub.
Verification is deliberately pass-through and leaves `verified=False`; a separate grounding
verifier is future work.

The graph stores normalized retrieved chunk objects in `RunState.retrieved` so a later
verifier can validate exact quotes and citation provenance. Retrieval failures and
five-second timeouts return empty chunks plus a `retrieval_error`; the graph records the
error and escalates rather than treating missing evidence as a successful retrieval.
Vector search uses the configured `CHROMA_URL` and `CHROMA_COLLECTION`. The worker also
reads the locally generated chunks file from `CORPUS_CHUNKS_PATH` for BM25 and exact clause
lookups. The default Compose configuration mounts `./corpus` read-only into the worker.

`search_corpus(query, k=5, doc_id=None, mode="vector")` returns a dictionary with
`chunks` and `retrieval_error`. Each chunk includes `clause_id`, `heading_path`,
`text`, `score`, and provenance fields. `get_clause(clause_id, doc_id)` returns the
exact normalized chunk from `chunks.jsonl`. Both tools have docstrings suitable for LLM tool
descriptions.

## Budgets

Defaults are 12 work steps, 15 retrieval-tool calls, and 60 elapsed seconds. The graph
checks for exhaustion before and after each work node. A failed retrieval stops retrying
and finalizes safely. Reaching a budget threshold produces an `ESCALATE` result with
`BUDGET_EXCEEDED`. Work steps and tool calls reserve a counter slot before executing.
Finalization is outside the work-step budget so it can always construct a valid result.

Elapsed time uses a monotonic clock. Tool calls are bounded at five seconds and return a
structured error rather than raising through the graph. The worker uses the same default
embedding function as the indexer when querying Chroma.

## Running and validation

```bash
uv run pytest -q tests/test_corpus_tools.py tests/test_graph.py
uv run ruff check .
uv run pytest -q
```

`run_agent(claim, correlation_id, run_id=...)` returns `(Result, trace)`. The queue worker
adapts the validated `Result` to the legacy `Recommendation` API/store payload, retaining
human approval requirements and `verified=False`. The public API contract remains unchanged.

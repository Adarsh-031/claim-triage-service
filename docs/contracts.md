# Shared data contracts

The shared contracts live in `libs/common/triage_common/contracts.py`. They define payloads shared by the API, agent graph and web client. JSON examples in `docs/examples/` are validated by `tests/test_contracts.py`.

Claims may include `one_way_basic_fare_inr` and `airline_fuel_charge_inr` as separate
calculator inputs, plus `sector`, `distance_km`, and
`ticket_cost_including_taxes_inr` for downgrade calculations. These are optional
evidence fields. Never derive them from `ticket_fare_inr`; a missing component leaves
the internal calculation in `needs_evidence`. Unverified calculator values are not
exposed as a final compensation recommendation.

## Chunk

A normalized source passage, including a stable `chunk_id`, document and clause identifiers, heading context, page metadata, cross-references, and a SHA-256 hash of the chunk text. The `chunk_id` is the key used to link citations to retrieved passages and must be present.

```json
{
  "chunk_id": "CAR-S3-M-IV-R4:3.3.2:0001",
  "clause_id": "3.3.2",
  "doc_id": "CAR-S3-M-IV-R4",
  "heading_path": ["Section 3", "Compensation", "Cancellation"],
  "text": "Example source passage used for retrieval.",
  "page": 12,
  "refs": ["3.3.1", "3.8.1"],
  "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Example file: [`docs/examples/chunk.json`](examples/chunk.json).

## Citation

A source quote supporting a result. `chunk_id` is required so the citation can be checked against the source chunk retrieved in the current run.

```json
{
  "clause_id": "3.3.2",
  "doc_id": "CAR-S3-M-IV-R4",
  "quote": "Example source passage used for retrieval.",
  "chunk_id": "CAR-S3-M-IV-R4:3.3.2:0001"
}
```

Example file: [`docs/examples/citation.json`](examples/citation.json).

## Result

The stable response contract emitted by the reasoning layer. The route is serialized as a plain string. Amounts are computed by code, not by the LLM, and a recommendation must still be reviewed by a human officer.

Route validation rules:

- `ESCALATE` requires `escalation_reason`; every other route forbids it.
- `PAY` requires an `amount` and at least one citation.
- `DECLINE` requires at least one citation.

```json
{
  "run_id": "f0e1d2c3-b4a5-4678-9012-abcdefabcdef",
  "route": "pay_as_computed",
  "summary": "Illustrative recommendation; a human officer must make the final decision.",
  "citations": [
    {
      "clause_id": "3.3.2",
      "doc_id": "CAR-S3-M-IV-R4",
      "quote": "Example source passage used for retrieval.",
      "chunk_id": "CAR-S3-M-IV-R4:3.3.2:0001"
    }
  ],
  "amount": 10000,
  "escalation_reason": null,
  "prompt_version": "v1",
  "corpus_version": "2026-10-24"
}
```

Example file: [`docs/examples/result.json`](examples/result.json).

## RunState

`RunState` is a partial `TypedDict` passed to LangGraph. The graph uses `claim`, `facts`,
`retrieved`, `draft`, and `result`, with `step_count`, `tool_calls`, a monotonic `started_at`,
and a sticky `budget_exceeded` flag for budget enforcement. `result` holds a serialized,
validated `Result`. The pass-through verifier leaves `verified=False`.

The graph returns `Result`; the queue worker adapts it to the existing `Recommendation`
API/store payload, preserving citation IDs, route, summary, amount, escalation reason, and
mandatory human approval. Migrating the public API/store to `Result` remains separate work.

See [the graph skeleton guide](agent-graph.md) for fixture provenance and budget semantics.

## Enums

- `Route`: member names `PAY`, `REQUEST_EVIDENCE`, `DECLINE`, `ESCALATE`. Wire values remain `pay_as_computed`, `request_further_evidence`, `decline`, and `escalate` for compatibility with the current API and worker.
- `EscalationReason`: `CAUSE_DISPUTED`, `PROVISION_CONFLICT`, `OUT_OF_CORPUS`, `VERIFICATION_FAILED`, `BUDGET_EXCEEDED`, `PROVIDER_DEGRADED`, `GUARDRAIL_BLOCKED`, and `WORKER_FAILURE`.

Older route and escalation member names remain aliases for backwards compatibility. Pydantic models reject unknown fields where strict validation matters. Generate the schema for the web client with `Result.model_json_schema()`.


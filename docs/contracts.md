# Shared data contracts

The shared contracts live in `libs/common/triage_common/contracts.py`. They define the payloads shared by the API, agent graph and web client. The JSON examples below are also stored in `docs/examples/` and validated by `tests/test_contracts.py`.

## Chunk

A normalized source passage, including document and clause identifiers, heading context, page metadata, cross-references, and a SHA-256 hash of the chunk text.

```json
{
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

A source quote supporting a result. `chunk_id` is optional for compatibility with existing recommendation payloads, but should be populated when available so verification can resolve the citation to a chunk retrieved in the current run.

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

`RunState` is a Python `TypedDict` passed directly to LangGraph. It is partial (`total=False`) because each node populates its own fields as the run progresses. It includes the existing graph state (claim, facts, retrieved passages, draft, verification status, attempts, steps, trace and recommendation) plus fields intended for the expanded workflow.

```python
{
    "claim": {"pnr": "AB12CD"},
    "facts": {"disruption_type": "cancellation"},
    "retrieved": [],
    "draft": None,
    "verified": False,
    "attempts": 0,
    "steps": 0,
    "tool_calls": 0,
    "trace": [],
    "recommendation": {}
}
```

## Enums

- `Route`: member names `PAY`, `REQUEST_EVIDENCE`, `DECLINE`, `ESCALATE`. Their wire values remain `pay_as_computed`, `request_further_evidence`, `decline`, and `escalate` for compatibility with the current API and worker.
- `EscalationReason`: `CAUSE_DISPUTED`, `PROVISION_CONFLICT`, `OUT_OF_CORPUS`, `VERIFICATION_FAILED`, `BUDGET_EXCEEDED`, `PROVIDER_DEGRADED`, `GUARDRAIL_BLOCKED`, and `WORKER_FAILURE`.

The older route and escalation member names remain aliases for backwards compatibility. Pydantic models reject unknown fields where strict validation matters. Generate the schema for the web client with `Result.model_json_schema()`.

# Shared data contracts

These examples document the shared schemas in `triage_common.contracts`. They are intended as valid JSON examples for API, worker, and client integration.

## Chunk

```json
{
  "clause_id": "3.3.2",
  "doc_id": "CAR-S3-M-IV-R4",
  "heading_path": ["Section 3", "Compensation", "Cancellation"],
  "text": "Example source passage used for retrieval.",
  "page": 12,
  "refs": ["CAR-S3-M-IV-R4:3.3.2"],
  "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

## Citation

```json
{
  "doc_id": "CAR-S3-M-IV-R4",
  "clause_id": "3.3.2",
  "quote": "Example source passage used for retrieval.",
  "chunk_id": "CAR-S3-M-IV-R4:3.3.2:0001"
}
```

## Result

```json
{
  "run_id": "f0e1d2c3-b4a5-4678-9012-abcdefabcdef",
  "route": "pay_as_computed",
  "summary": "The claim meets the documented criteria, subject to human review.",
  "citations": [
    {
      "doc_id": "CAR-S3-M-IV-R4",
      "clause_id": "3.3.2",
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

## RunState

`RunState` is a Python `TypedDict` used inside the LangGraph graph, not an API payload. It is intentionally partial (`total=False`) because graph nodes populate state as the run progresses.

```python
{
    "run_id": "f0e1d2c3-b4a5-4678-9012-abcdefabcdef",
    "claim": {"pnr": "AB12CD"},
    "chunks": [],
    "citations": [],
    "route": "pay_as_computed",
    "summary": "Pending human review.",
    "amount": 10000,
    "escalation_reason": None,
    "prompt_version": "v1",
    "corpus_version": "2026-10-24",
    "verification_passed": True,
    "errors": []
}
```

## Enums

- `Route`: `pay_as_computed`, `request_further_evidence`, `decline`, `escalate`.
- `EscalationReason`: `DISPUTED_CAUSE`, `PROVISION_CONFLICT`, `OUT_OF_CORPUS`, `VERIFICATION_FAILED`, `BUDGET_EXCEEDED`, `PROVIDER_DEGRADED`, `GUARDRAIL_BLOCKED`, `WORKER_FAILURE`.

All Pydantic models reject unexpected fields where strict validation is important. The `Result` schema can be generated for consumers with `Result.model_json_schema()`.

# Bounded research graph (issue #26)

The worker runs `intake → research ↺ → draft → verify → finalize`. Research calls the
configured OpenAI-compatible provider with the tool definitions and a conversation
containing previous tool results. Each assistant turn may request several tools.
Conditional edges continue research, draft when the provider finishes, or finalize an
escalation on an exhausted budget or unavailable corpus/provider. The draft node also
routes provision conflicts directly to finalization.

The `intake-v1` stage extracts disruption, notice/block time, alternate offered/accepted,
contact availability, and stated cause from the narrative and form. Non-null form fields
take precedence; disagreements are retained in the intake trace. Invalid output gets one
validation retry within the remaining run deadline, then escalates. Provider failures use
`PROVIDER_DEGRADED`; unsupported origins use `OUT_OF_CORPUS` before any provider or retrieval
call. Research checklists, claim-field tools and compensation all use a reconciled copy
of the claim, preserving booking/fare data and alternate departure timing locally.

Intake sends only extraction fields and the narrative to the provider. With `LLM_PROVIDER=stub`,
intake uses a deterministic in-process form reader; research still uses the HTTP stub.
Narrative extraction/conflict tests inject scripted intake responses. Provider objects stay
outside graph state. Results identify both stages and the selected research variant as
`intake-v1+research-v1+v1` (or `+v2`).

Intake supplies an explicit allowlist of eligibility facts and a data checklist:
airline code, disruption type, block time, notice and delay durations, airport reporting,
contact-info availability, stated cause, and alternate offered/accepted/departure delay.
The claim-field tool enforces the same allowlist, including filtering the entire alternate
object. Booking PNR, flight number/date, origin/destination, total fare, narrative, and
new unlisted fields stay local during research. The model chooses
retrieval tools; it cannot replace structured facts, waive checklist coverage, supply
calculator inputs, or set the output amount. The versioned instructions are in
`prompts/research_v1.md` (`prompt_version=research-v1`).

## Tools and evidence

The tools are `search_corpus`, `get_clause`, `get_references`, `get_referrers`,
`get_definition`, `get_claim_field`, and `compute_compensation`. Arguments validate
against registered schemas; unknown tools and invalid arguments return tool errors
for the next research turn. They still consume a tool reservation. Tool spans and
trace entries include the tool name, arguments, duration, returned chunk IDs, and
reference edges. Claim-field values are not copied into tool trace entries.

Retrieval reads ingestion's `chunks.jsonl`, `refs.json`, and `definitions.json`.
Exact lookups and graph edges retain document-qualified IDs, including unresolved
edges; an unresolved child returns no chunk rather than guessing a match. Definition
records resolve to full source chunks, whose text is used for exact citations.
Chunk hashes are checked when loading; the corpus version fingerprints all three
artifacts. A changed corpus between research turns fails closed. Ingestion must finish
before consumers start.

Search currently ranks the processed chunks by lexical term overlap, with deterministic
tie-breaking and document filters. It does not query Chroma; semantic/hybrid ranking is
separate retrieval work. There is no fallback to the old hand-written `DEMO-CAR` passages.
Missing processed artifacts produce `OUT_OF_CORPUS`.

Checklists cover cancellation, delay, denied boarding, and downgrade. Cancellation
requires notice, alternate offered/accepted, contact details, extraordinary circumstances,
and meals/facilities. The server derives coverage from actual retrieved chunks and
structured claim fields. On completion every entry is `cited` or explicitly `missing`,
with missing clause IDs and field names. Missing evidence never becomes an assumed fact.
For IndiGo claims both CoC snapshots are examined: the current claim contract has no
jurisdiction field, so the worker does not infer domestic/international scope from IATA codes.
Some checklist facts are not yet collectable through `ClaimIn`; they remain missing.

## Demonstrated behavior and verification boundary

The local HTTP LLM stub scripts cancellation research through all seven tools. Tests
exercise that same HTTP response path in-process, with ingestion artifacts generated
from the pinned CAR text fixture and both CoC snapshots. They need no external model,
network service, PDF binary, or pre-existing `corpus/processed` directory.

For s01, `compute_compensation` returns zero because the alternate was accepted.
The research draft requests further evidence/review because the CAR/CoC relationship
has not been independently validated; it does not assert a decline or meals entitlement.
The missing stated cause is recorded explicitly. The scripted run uses 8 work steps
and 14 tool calls.

For a technical-fault cancellation, CoC `1.1` explicitly lists technical failures while
CAR `1.4` requires beyond-control and reasonable-measures evidence. The worker refers
this potential lower-protection interpretation as `PROVISION_CONFLICT`, with both source
citations. It does not conclude that every technical fault qualifies for compensation.
Provider comparisons must cite CAR `1.4`/`1.5` and IndiGo `1.1`/`14.2`, with all six
comparison clauses retrieved across CAR and both CoC snapshots. Unrelated retrieved
chunks are rejected. Even relevant citations only establish coverage: model-authored
`compatible` and `conflict` reports become `missing` pending independent relationship
validation, and the comparison checklist remains missing. Such reports cannot trigger
`PROVISION_CONFLICT`; only the deterministic technical-fault review condition above can.
The s21 expected route reflects the explicit escalation requirement in issue #26.

The separate grounding verifier is still pending. `verify` leaves `verified=False`,
and finalization converts every consequential draft into `VERIFICATION_FAILED` with
an explicitly unverified research summary and citations. No final decline/payment is
published. The worker preserves its existing `Recommendation` API contract, run ID,
`verified=False`, and `requires_human_approval=True`.

The full calculator in #15/PR #36 is not merged into this branch. A cancellation adapter
provides structured `status`, `amount`, `formula`, `clause_id`, `inputs_used`, and
`missing_fields`, including the accepted-alternate zero case. It never substitutes
`ticket_fare_inr` for basic fare plus fuel. Missing fare components need evidence;
other disruption calculations await #15. The registry accepts #15's `to_dict()` result
shape when integrating that implementation. Positive payment drafting and broader
eligibility reasoning remain separate from this cancellation research milestone.

## Budgets and runtime

Defaults remain 12 work steps, 15 tool reservations, and 60 elapsed seconds. Counters
reserve before execution; reaching the threshold skips execution and finalizes
`BUDGET_EXCEEDED`. Thus at most 11 work nodes and 14 tools execute with default limits.
Every tool in a batched response reserves separately. Time is checked after each tool
and provider turn, so an expired operation cannot start the next tool or drafting.
Finalization runs outside the work-step budget and budget exhaustion takes precedence.
LangGraph's recursion limit remains `agent_max_steps + 2`.

Provider requests receive the remaining run time, capped by `LLM_TIMEOUT_S`. Retry,
backoff, and fallback share the same remaining deadline. Local artifact reads use
cooperative checks between operations; the graph does not forcibly interrupt filesystem
I/O. The HTTP stub keeps the generic `STUB` response for callers without research tools.

Compose mounts the ingested corpus read-only into workers after ingestion succeeds.
The worker image copies the prompt and configures absolute corpus/prompt paths because
its Python package is installed outside `/src`. `CORPUS_DIR` can override the processed
artifact directory; `RESEARCH_PROMPT_PATH` can override the prompt file for local runs.
`INTAKE_PROMPT_PATH` selects the intake prompt; the worker image sets it to
`/src/prompts/intake_v1.md` so installed packages can load the asset.

```sh
uv run ruff check .
uv run pytest -q
```

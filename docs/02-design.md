# 02 - System Design (v1.0, 2026-10-24)

Air Passenger Compensation Claim Triage Service. Inputs: a passenger claim. Output: a cited entitlement
recommendation routed to `pay_as_computed`, `request_further_evidence`, `decline`, or `escalate`.
**The system never authorises payment; a customer relations officer decides.**

Requirement IDs (F, A, L, D, demo items) refer to `MiniProject_04.pdf`; FR/NFR IDs refer to `01-requirements.md`.

---

## 1. Decisions locked in this version

| Area | Decision | Why |
|---|---|---|
| Airline document | **IndiGo Conditions of Carriage** | Real conflicts with the CAR (technical faults as force majeure; non-encashable vouchers) give genuine escalation cases; Vietnam section gives an out-of-jurisdiction test |
| Regulation | DGCA CAR Sec 3, Series M, Part IV, **Rev 4 (eff. 15 Feb 2023)** | Mandated corpus |
| Agent framework | **LangGraph** | Explicit state graph with loops, bounded recursion, checkpointable state (A1) |
| Retrieval framework | **LangChain** (`langchain-chroma`, `langchain-huggingface`, BM25 retriever) | Brief L1 requires LangChain or LlamaIndex; integrates directly with LangGraph tools |
| Vector store | **Chroma** (own container) | Brief L1 requires FAISS or Chroma. Chroma runs as a server, so every scaled worker shares one index and we can upsert individual chunks (partial re-embed). **Uses the project’s shared vector-store service.** |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, local, CPU | Free, small (384-dim), brief §12 |
| LLM | OpenRouter free model (tool-calling capable) → second free model as fallback → offline stub | Free tier; provider swappable by config (D4, §12) |
| Queue | Redis lists (`BLMOVE` reliable-queue pattern) | Already needed for cache + rate limit; no extra broker |
| Database | PostgreSQL | Runs, steps, quarantine, feedback, cost, corpus versions in one place; supports run reconstruction (D5) |
| Async pattern | **Submit → 202 + run_id → client polls** `GET /v1/runs/{id}` | Web client needs no public callback URL; polling is trivially debuggable and load-testable with Locust. Optional `callback_url` is a P2 extra. |
| Tracing | OpenTelemetry → **Jaeger** | Simplest single-container trace UI |
| Evaluation | **TruLens** feedback functions + our own deterministic route/citation checks | L3 |
| Load test | **Locust** against the stack with `LLM_PROVIDER=stub` | D6 |
| Python tooling | uv workspace, one `uv.lock` | Reproducible builds |

---

## 2. Containers and deployment boundaries (D3)

```
            ┌──────────┐   POST /v1/claims    ┌──────────┐  enqueue   ┌─────────┐
 officer ──►│   web    │ ───────────────────► │   api    │ ─────────► │  redis  │
            └──────────┘ ◄── poll /v1/runs ── └────┬─────┘            └────┬────┘
                                                   │ read/write            │ q:research / q:verify
                                              ┌────▼─────┐          ┌──────▼────────┐  ┌───────────────┐
                                              │ postgres │◄─────────│ agent-worker  │─►│ verifier-     │
                                              └──────────┘          │  (x N)        │  │ worker        │
                                                   ▲                └──┬─────┬──────┘  └──┬─────┬──────┘
                                                   │                   │     │            │     │
                                   ┌──────────┐    │            ┌──────▼─┐ ┌─▼──────────────▼┐  │
                                   │  ingest  │────┴───────────►│ chroma │ │ LLM: OpenRouter │◄─┘
                                   │ (one-off)│                 └────────┘ │  or llm-stub    │
                                   └──────────┘                            └─────────────────┘
   jaeger ◄── OTLP traces from api / workers        prometheus ◄── /metrics + cadvisor ──► grafana
```

| Service | Role | Boundary justification |
|---|---|---|
| `api` | FastAPI: contract validation, quarantine, rate limit, enqueue, run status, trace, feedback, metrics | Light, latency-sensitive; must stay up when agents are slow (failure isolation) |
| `agent-worker` | Intake agent + Research agent (LangGraph) | The CPU/IO-heavy part; the thing we `--scale` (A7) |
| `verifier-worker` | Verifier agent | Separate process = real hand-off and independent failure (A2); can be scaled or released separately |
| `redis` | Queues, retrieval/run cache, per-client rate-limit counters | Shared state for horizontally scaled workers |
| `postgres` | System of record | Persistence independent of compute |
| `chroma` | Vector index | One shared index for all workers; different resource profile (memory) |
| `llm-stub` | Fixed responses after a configurable realistic delay | Load tests measure our system, not provider limits (D6) |
| `web` | Static officer client (nginx) | Independent release of UI |
| `ingest` | One-shot CLI container that runs automatically before workers; can also be run with `docker compose run --rm ingest` | Fetches and verifies the corpus before worker startup |
| `jaeger`, `prometheus`, `grafana`, `cadvisor` | Observability | Standard images; per-container resource metrics from cadvisor |

Every long-running service declares a `healthcheck`; `depends_on: condition: service_healthy` orders start-up so
`docker compose up` works from a clean clone (demo 1). The one-shot ingest service fetches and verifies the corpus
before workers start; run it manually with `docker compose run --rm ingest` when needed. Nothing runs on the host.

---

## 3. Request lifecycle

1. **Ingestion boundary (api).** Pydantic contract `claim-v1` (`extra="forbid"`) checks schema, types, ranges,
   enums and cross-field rules. On failure → row in `quarantine` with field-level errors, **HTTP 422, no run row,
   no enqueue** (metric `agent_runs_started_total` unchanged → demo 4).
2. **Admission.** Per-client token bucket in Redis (default 10 runs/min per API key, D7). Whole-run cache lookup by
   `sha256(normalised claim + prompt_version + corpus_version)`; a hit returns a completed run immediately.
3. **Enqueue.** Insert `runs` row (`queued`), push `{run_id, correlation_id}` to `q:research`, return
   `202 {run_id, poll_url}`.
4. **Agent run (agent-worker).** Intake agent → Research agent → writes a `Draft` → pushes to `q:verify`.
5. **Verification (verifier-worker).** Verifier agent returns `verified`, `retry(strategy)` or `escalate(reason)`.
   Retry sends the run back to `q:research` with the verifier's critique and a different retrieval strategy.
6. **Finalise.** Verified draft → `Recommendation` (status `succeeded`) or escalation (status `escalated`).
   `requires_human_approval` is always `true`.
7. **Poll.** Web client polls `GET /v1/runs/{id}` every 1.5 s; trace at `GET /v1/runs/{id}/trace`.

---

## 4. Agent topology (A1, A2, A3)

Three agents with distinct responsibilities, tools and prompts. The Intake and Research agents share a process; the
Verifier runs in its own container, so the hand-off is a persisted contract over a queue, not a function call.

### 4.1 Intake agent (agent-worker)
- **Job:** guardrail screening, disruption classification, entity extraction (F3).
- **Inputs:** validated claim. **Output:** `IntakeResult {category, entities, field_conflicts[], injection_flag, toxicity_flag}`.
- Structured fields win over the narrative; disagreements become `field_conflicts` (e.g. form says "delay",
  statement says "cancelled") and push the route towards `request_further_evidence`.
- Injection flag → the narrative is withheld from later prompts (only structured fields pass on) and the trace
  records the guardrail hit (demo 7).

### 4.2 Research agent (agent-worker) — the tool-calling loop
- **Job:** find the primary provision **and every condition, cross-reference, definition and exemption that could
  modify it**, then draft an entitlement position (F4, F5).
- Implemented as a LangGraph ReAct-style node: the model chooses tools; retrieval is never a fixed stage (A1).
- **Required reasoning pattern (brief §5):** (1) retrieve apparent provision → (2) ask "what modifies this?" and
  follow references → (3) re-query from what it read → (4) hand to verifier → (5) retry differently or escalate.
- A per-category **condition checklist** (data, not prompt) lists what must be resolved, e.g. cancellation:
  `notice_period (3.3.1)`, `alternate_offered/accepted (3.3.2)`, `contact_info (3.3.3)`,
  `extraordinary_circumstances (3.3.4 → 1.4/1.5)`, `facilities (3.8.1)`. The agent cannot draft until every item
  has a cited resolution or is marked `unknown → evidence needed`.
- **Output:** `Draft {route, entitlements[], compensation_call?, citations[], checklist_state, reasoning_summary}`.

### 4.3 Verifier agent (verifier-worker)
Deterministic checks first, LLM judgement second:
1. Every citation's `chunk_id` exists in `retrieved_chunks` **for this run** and its `quote` is a substring of that
   chunk's text (hard gate, A3).
2. Every checklist item is resolved or explicitly flagged.
3. Any amount equals the output of `compute_compensation` for the cited clause (the LLM never does arithmetic).
4. Output filter: no payment authorisation language; route ∈ allowed set (L4).
5. LLM entailment check: does each cited passage support the sentence it is attached to?

Result: `verified` | `retry(strategy, critique)` | `escalate(VERIFICATION_FAILED)`.
Retry strategies in order: **graph expansion** (pull all referenced/referring clauses) → **keyword/BM25 search** →
escalate. Maximum 2 retries (demo 6).

### 4.4 Hand-off contract and failure mid-run
- Hand-off = `Draft` row in Postgres + message on `q:verify`. Workers use a reliable queue (`BLMOVE` to a
  processing list); a reaper re-queues messages whose worker died (heartbeat > 60 s).
- Agent-worker dies mid-run → message re-queued once; run restarts from the last persisted step. Second failure →
  `escalate(WORKER_FAILURE)`.
- Verifier unavailable → draft waits on `q:verify`; if not verified within the run wall-clock budget →
  `escalate(VERIFICATION_UNAVAILABLE)`. **An unverified draft is never returned.**
- LLM provider down → see §8 fallback chain.

### 4.5 Loop bounds (A1)
| Bound | Limit | On breach |
|---|---|---|
| Graph steps per run | 12 | escalate `BUDGET_EXCEEDED` |
| Tool calls per run | 15 | escalate `BUDGET_EXCEEDED` |
| Research↔verify retries | 2 | escalate `VERIFICATION_FAILED` |
| Wall clock per run | 60 s | escalate `BUDGET_EXCEEDED` |
| Per LLM call timeout | 20 s | retry/backoff, then breaker |
| Per tool call timeout | 5 s | tool error returned to the agent |

---

## 5. Tools (A1: at least three)

| Tool | Signature | Notes |
|---|---|---|
| `search_corpus` | `(query, doc_filter?, k=6, mode="dense"\|"bm25"\|"hybrid") -> [Chunk]` | LangChain retrievers over Chroma + BM25; results logged to `retrieved_chunks` |
| `get_clause` | `(clause_id, doc_id="CAR") -> Chunk` | Exact lookup by ID, e.g. `3.8.1(a)` |
| `get_references` | `(clause_id) -> [clause_id]` | Clauses this one cites (3.3.2 → 3.3.1, 3.8.1(a)) |
| `get_referrers` | `(clause_id) -> [clause_id]` | Clauses that cite this one (finds modifiers/exemptions) |
| `get_definition` | `(term) -> Chunk` | Section 2 definitions, e.g. "block time" |
| `get_claim_field` | `(name) -> value` | The claim's own validated fields |
| `compute_compensation` | `(clause_id, inputs) -> {status, amount, formula, clause_id, inputs_used, missing_fields}` | Deterministic calculator exists; current LangGraph skeleton does not register or invoke it. See [compensation workflow status](compensation.md#workflow-integration-status). |

Every tool call is a traced span and a `steps` row (input, output size, latency).

---

## 6. Corpus, chunking and index (L1, D2)

### 6.1 Documents and manifest
`corpus/manifest.yaml`: `doc_id`, title, source URL, version/effective date, sha256, precedence
(`CAR=binding_floor`, `INDIGO=airline_terms`), jurisdiction.

### 6.2 Chunking strategy (answers brief §3 "amounts are easy, conditions are hard")
- **One complete chunk per numbered clause**, including `3.3.2`, without sentence or token splitting.
  CAR lettered subclauses also have addressable child views (e.g. `3.8.1(a)`); their text overlaps the parent.
  This preserves the whole compensation rule while allowing precise reference targets. See
  [ingestion](ingestion.md) for the implemented export format.
- **Contextual header** prepended before embedding: `CAR Rev4 > 3 Requirements > 3.3 Cancellation > 3.3.2`. Keeps
  "compensation amounts" chunks semantically tied to their conditions.
- **Metadata:** `doc_id, clause_id, parent_id, heading_path, refs_out[], defined_terms[], precedence, version, chunk_sha`.
- **Cross-reference extraction:** regex over "Para 3.8.1 (a)", "Para 1.4 and 1.5", "sub-para (b)" → `clause_refs`
  table (edges). This graph is what lets the agent reach 3.8.1(a) from 3.3.2 (demo 10).
- **Definitions** (CAR Section 2) indexed as their own chunks and linked to terms found in other clauses.
- IndiGo chunks carry `precedence=airline_terms`, so conflict detection can compare like clauses.

### 6.3 Idempotent pipeline (automatic one-shot; also `docker compose run --rm ingest`)
read → parse (pdfplumber; OCR only if no text layer) → chunk → hash → **embed only chunks whose `chunk_sha`
changed** → upsert to Chroma by `chunk_id` → delete orphaned chunks → write `corpus_versions` row
(built_at, doc versions, chunk count, docs/s, chunks/s). Re-running on unchanged files does nothing. Freshness and
throughput exposed as Prometheus gauges.

---

## 7. Precedence and escalation rules (F6, F7)

- **CAR is the floor.** IndiGo terms may add entitlements, never reduce them.
- Airline clause less generous than CAR on the same point → **escalate `PROVISION_CONFLICT`** with both citations.
- A technical-snag label alone does not establish Force Majeure: IndiGo's CoC names technical failures, while CAR 1.4/1.5 use broad beyond-control language and require that reasonable measures could not avoid the event. Request evidence that applies these shared conditions; use `CAUSE_DISPUTED` if evidence about the cause or avoidability conflicts.
- Stated cause disputed or unverifiable and it decides compensation → `CAUSE_DISPUTED`.
- Route outside the corpus (foreign carrier, IndiGo Vietnam-jurisdiction flights, diversion) → `OUT_OF_CORPUS`.
- Missing a fact the checklist needs (e.g. contact details given?) → `request_further_evidence`, naming it.
- `decline` must carry at least one citation (AirSewa requirement).

Escalation reasons: `CAUSE_DISPUTED, PROVISION_CONFLICT, OUT_OF_CORPUS, VERIFICATION_FAILED, BUDGET_EXCEEDED,
PROVIDER_DEGRADED, WORKER_FAILURE, VERIFICATION_UNAVAILABLE, GUARDRAIL_BLOCKED`.

### 7.1 Worked example: brief §4.1
Claim: cancellation, notice 90 min, alternate offered and accepted (+4 h).
1. `search_corpus("cancellation compensation")` → 3.3.2 (amounts).
2. `get_references("3.3.2")` → 3.3.1 (notice), 3.8.1(a) (facilities).
3. `get_clause("3.3.1")` → notice < 24 h, so 3.3.2 applies.
4. Reading 3.3.2: airline provides **either** an acceptable alternate flight **or** compensation + refund. Passenger
   accepted the alternate → no cancellation compensation.
5. `get_clause("3.8.1(a)")` → meals/refreshments while waiting are still owed.
6. Draft: `decline` (compensation) citing 3.3.2 + entitlement to meals/refreshments citing 3.8.1(a). Verifier
   confirms both quotes resolve → `succeeded`.

---

## 8. Reliability (A5)

- **Timeouts** on every LLM (20 s) and tool (5 s) call.
- **Retry:** max 3, exponential backoff with full jitter (0.5 s base, 8 s cap), only on timeouts/429/5xx.
- **Circuit breaker per provider:** opens after 5 failures in 30 s; half-open probe after 30 s; state exported as a
  metric and shown on the dashboard.
- **Fallback chain:** primary free model → secondary free model → `escalate(PROVIDER_DEGRADED)`. A run that did not
  complete is never returned as a confident answer (demo 8).
- Demonstrable on request: `LLM_FAULT_INJECTION=timeout|500|429` env switch on the stub and an admin endpoint to
  force the breaker open.

---

## 9. Guardrails (L4)

| Layer | Check |
|---|---|
| Input (api + intake) | Length limits, control-char stripping; injection classifier (rules + small model/LLM check) on `statement` and `stated_cause`; toxicity check |
| Prompting | Narrative passed as quoted data in a delimited block; system prompt states it is untrusted; tools are read-only |
| Output (verifier) | Route whitelist; forbid authorisation language ("approved", "we will pay"); amounts only from `compute_compensation`; toxicity check |

---

## 10. Data model (PostgreSQL)

`runs(run_id, correlation_id, client_id, status, route, escalation_reason, prompt_version, corpus_version, model, created_at, finished_at, result_json)`
`steps(run_id, seq, agent, node, tool, input_json, output_summary, latency_ms, started_at)`
`retrieved_chunks(run_id, chunk_id, doc_id, clause_id, query, rank, score)`
`llm_calls(run_id, step_seq, provider, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, cached)`
`drafts(run_id, attempt, draft_json, verifier_verdict, critique)`
`quarantine(id, received_at, client_id, payload_json, errors_json)`
`feedback(run_id, satisfactory, reason, created_at)`
`corpus_versions(version, built_at, docs_json, chunk_count, docs_per_s, chunks_per_s)`
`clause_refs(src_clause, dst_clause, doc_id)`

`GET /v1/runs/{id}/trace` joins these to reconstruct a run: every tool call, query, passage, prompt version, per-step
latency and cost, and the verifier verdict (D5, demo 3).

---

## 11. Observability, cost and feedback (F8, A6, D5, L5, L6)

- **Logs:** JSON (structlog) with `correlation_id`, `run_id`, `agent`, `step`.
- **Traces:** OTel spans per HTTP request, queue hop (context propagated in the message), agent node, tool and LLM call.
- **Metrics:** `runs_total{route,status}`, `run_duration_seconds` (histogram → p50/p95), `agent_steps_per_run`,
  `tool_calls_per_run`, `llm_retries_total`, `circuit_state`, `llm_tokens_total`, `llm_cost_usd_total{step}`,
  `quarantine_total`, `corpus_chunks`, `corpus_built_timestamp`, plus cadvisor container CPU/memory/uptime.
- **Grafana:** one service dashboard (latency, error rate, throughput, availability, per-container resources) and
  one agent dashboard (success rate, steps, tool calls, per-step latency, retry rate, cost).
- **Cost (L5):** tokens and cost recorded at call time per step; report cost per 100 runs and top cost driver.
- **Feedback (L6):** `POST /v1/runs/{id}/feedback`; a scheduled job scores a 10% sample of live runs with the eval
  metrics and writes a quality time series to Prometheus.

---

## 12. Caching and rate limiting (D7)

| Cache | Key | Why |
|---|---|---|
| Embeddings | `chunk_sha` | Ingest only re-embeds changed chunks |
| Query embeddings + retrieval results | `sha256(query, mode, k, corpus_version)` | Agents repeat similar queries across runs |
| Whole run | `sha256(claim, prompt_version, corpus_version)` | Identical resubmissions cost nothing |

Rate limit: Redis token bucket per API key. Report p95 and cost per run with caches on vs off.

---

## 13. Performance plan (D6, A7)

- **SLO declared now, before any measurement:** p95 end-to-end (submit → final result) ≤ **25 s** at **10 concurrent
  runs**, LLM stub at ~1.5 s per call, ≤ 8 LLM calls per run. Submission call p95 ≤ 200 ms.
- Locust: ramp users, record RPS, p50, p95 of complete runs; find the concurrency where p95 > 25 s and the
  responsible component (queue wait vs worker CPU vs Chroma vs Postgres).
- Repeat with `--scale agent-worker=3`; if no improvement, identify the bottleneck from traces/cadvisor.

---

## 14. Evaluation (L2, L3)

- `eval/scenarios/*.json`: ≥ 20 cases, each with expected route, expected escalation reason (if any) and the
  supporting clause IDs. Includes §4.1, extraordinary circumstances, contact-info exclusion, denied boarding 1 h/24 h
  bands, long delay facilities, downgrading, CAR-vs-IndiGo conflict, Vietnam route, injection attempt.
- `uv run python cli/evaluate.py --prompt-version v1|v2`: route accuracy, citation precision/recall, TruLens
  groundedness, context relevance, faithfulness (answer relevance to passages), hallucination rate
  (= share of answer sentences not entailed by any retrieved passage).
- Prompts in `prompts/<agent>/<version>.md`; version stamped on every run. Compare v1 vs v2; then one deliberate
  change (e.g. graph expansion on/off) with all four metrics before/after.

---

## 15. Rejected alternatives

| Rejected | Reason |
|---|---|
| Single-pass RAG (embed → top-k → answer) | Misses provisions the question never mentions; fails §4.1 by design |
| FAISS | In-process index means each scaled worker holds its own copy and partial updates are clumsy; Chroma server is shared |
| CrewAI | Less control over explicit loops, bounds and state persistence than LangGraph |
| Celery | Extra moving parts for a two-queue system; Redis lists + reliable-queue pattern suffice |
| Webhooks as the only completion path | Web client and Locust would need a public receiver; polling is simpler |
| LLM computing amounts | Arithmetic errors; amounts are deterministic code keyed to the clause |
| Paid embedding API | Only real cost in the project (brief §12); local MiniLM is enough |

---

## 16. Open items for v1.1
- Verify compensation tiers in `tools/compensation.py` against CAR 3.3.2 / 3.2.2 / 3.5 text.
- Choose exact primary/secondary OpenRouter free models (must support tool calling).
- Regenerate `img/architecture.png` with Chroma, verifier-worker, llm-stub, cadvisor.
- Team ownership per section (agents / data+eval / platform).

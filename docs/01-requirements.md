# MP04 Air Passenger Claim Triage — Phase 1: Problem, Approach, Requirements (draft v0.1, 2026-10-19)

## 1. Problem framing
- Input: passenger claim (structured fields + free-text narrative). Output: entitlement recommendation (route + amount + facilities + clause citations) for a human officer. Never authorises payment.
- Core difficulty: entitlement is a conjunction of conditions spread across clauses that cross-reference each other (3.3.2 -> 3.3.1 notice, 3.3.3 contact info, 3.3.4 -> 1.4/1.5 extraordinary circumstances, 3.8.1(a) facilities, 2.x definitions). Similarity search finds amounts, misses the conditions.
- Section 4.1 case: informed 90 min before -> not informed per 3.3.1 -> 3.3.2 applies, but 3.3.2 is EITHER acceptable alternate flight OR compensation + refund. Passenger took alternate -> no compensation; still owed meals/refreshments under 3.8.1(a) (reported at airport, waiting). Route: decline compensation citing 3.3.2, with facilities entitlement under 3.8.1(a).

## 2. Approach (key design bets)
1. Structure-aware corpus: chunk per clause (3.3.2(a)...), each chunk carries clause_id, parent heading, doc/version. At ingest, regex-parse cross-references ("Para 3.8.1 (a)", "Para 1.4 and Para 1.5") into a clause reference graph. Definitions (Sec 2) linked to terms.
2. Agent decides retrieval: tools = search_corpus, get_clause(id), get_references(id) / get_referrers(id), get_claim_field(name), compute_compensation(rule, inputs). Loop: retrieve apparent provision -> look for modifiers/exemptions -> re-query from what it read -> verify -> retry/escalate.
3. LLM interprets, code computes: amounts (min(cap, fare+fuel), % of ticket) are deterministic functions keyed by clause; the LLM never does arithmetic.
4. Separate verifier agent: every claim-sentence in the answer must map to a citation whose text was actually retrieved in this run; condition checklist per disruption type must be fully resolved. Fail -> retry with different strategy (graph expansion / keyword BM25) -> escalate.
5. Escalation is a first-class outcome, not an error: disputed cause attribution, conflicting provisions (CAR vs charter), out-of-corpus (foreign carrier 3.6, diversion), budget exhausted, verification failed.
6. Precedence rule: CAR is binding floor; airline charter may add, never subtract. Conflict where charter is less generous -> CAR wins, flagged.

## 3. Functional requirements (refined, testable)
- FR-1 Claim intake (F1/D1): POST /v1/claims against versioned JSON Schema (claim-v1). Returns 202 + run_id. OpenAPI published.
- FR-2 Validation & quarantine (F2/D1): schema, type, range (block_time 0-20h, notice >= 0), enums, cross-field (alternate_offered=false => alternate_taken must be false; delay requires actual_departure > scheduled; downgrade requires booked_class > flown_class). Rejects -> quarantine table with field-level errors, 422 response, no run row, no agent started (metric: agent_runs_started unchanged).
- FR-3 Disruption classification & entity extraction (F3): category in {denied_boarding, cancellation, delay, downgrading, unknown}; entities: notice_period, block_time, alternate_flight (offered/taken/departure delta), contact_details_provided, reported_at_airport, stated_cause. Narrative vs structured field conflicts flagged.
- FR-4 Provision retrieval with conditions (F4): for the category, retrieve primary provision + all referenced/referring clauses + exemptions + definitions. Coverage check against a per-category condition checklist.
- FR-5 Grounded entitlement position (F5): compensation (amount, formula, clause), refund, facilities (3.8.1 a/b), each with clause-level citations resolving to retrieved text.
- FR-6 Routing (F6): pay_as_computed | request_further_evidence (names the missing evidence) | decline (cites provision). Basis stated.
- FR-7 Escalation (F7): escalate_to_human with reason code (CAUSE_DISPUTED, PROVISION_CONFLICT, OUT_OF_CORPUS, VERIFICATION_FAILED, BUDGET_EXCEEDED, PROVIDER_DEGRADED).
- FR-8 Run status & result: GET /v1/runs/{id} (queued/running/succeeded/escalated/failed); full trace at GET /v1/runs/{id}/trace.
- FR-9 Feedback (L6): POST /v1/runs/{id}/feedback {satisfactory, reason}; aggregated metrics.
- FR-10 Corpus pipeline (D2): CLI ingest (read, parse, chunk, embed, index), idempotent by content hash; partial re-embed.
- FR-11 Evaluation (L3/L2): CLI batch over >= 20 scenarios; per-prompt-version metrics.
- FR-12 Web client (A8): submit, poll, show route, citations, escalation.
- FR-13 Ops endpoints (F8): /healthz, /readyz, /metrics on every service.

## 4. Non-functional requirements (targets declared before measurement)
- Latency SLO (D6): p95 end-to-end (submit -> final result) <= 25 s at 10 concurrent runs, LLM stub at ~1.5 s/call, <= 8 LLM calls/run. Submission call p95 <= 200 ms.
- Throughput: >= 0.5 completed runs/s with 3 workers on stub (to be validated).
- Bounded agent: <= 12 steps, <= 15 tool calls, <= 60 s wall clock per run; per-call timeout 20 s LLM, 5 s tools.
- Reliability (A5): retry x3 exponential backoff + jitter; circuit breaker (open after 5 failures/30 s, half-open after 30 s); fallback chain primary -> secondary provider -> escalate (never a confident answer from an incomplete run).
- Correctness: 0 uncited/unresolvable citations returned (hard gate); hallucination rate <= 5% on eval set; 4.1 case must pass.
- Security (L4): injection screening on narrative; output filter forbids "approve payment"/amounts not produced by compute tool; toxicity check in/out; no secrets in repo (D4).
- Observability (D5): JSON logs with correlation_id across services; OTel traces -> Jaeger; Prometheus on all services; Grafana dashboard; any run reconstructible from run_id.
- Cost (L5/D7): tokens + cost per step and run; target <= Rs X per 100 runs on free tier (set after provider choice); per-client rate limit (e.g. 10 runs/min); cache embeddings + retrievals + whole-run by claim hash.
- Portability/Deployability (D3): single docker compose up from clean clone, >= 4 services, health checks, --scale worker=3.
- Maintainability: prompts as versioned files; model + provider by config only.

## 5. Prioritisation (zero marks if no working E2E)
- P0 walking skeleton by end of week 2: compose up, API + queue + 1 worker, ingest CAR, single-agent loop with 3 tools, poll result, 4.1 case passes.
- P1: verifier agent + escalation, validation/quarantine, tracing/logging, eval set (20) + metrics, circuit breaker/fallback, web client.
- P2: Grafana dashboard polish, Locust + scaling study, caching/rate limit measurement, prompt A/B, feedback + live scoring, runbook/incident log.

## 6. Decisions
- LLM: OpenRouter free model (user, Oct 20 2026). Implications: free-model daily request cap (low unless $10 credits bought), so stub LLM for dev/tests/load, aggressive caching, a second free model as fallback in the breaker chain. Model must support tool calling.
- Airline: undecided. Recommended IndiGo: its Conditions of Carriage list "mechanical, technical, operational issues" and "shortage of manpower/parts" as beyond its control, while CAR 1.4/1.5 doesn't, giving a real CAR-vs-charter conflict to demo PROVISION_CONFLICT escalation. Alternative Air India (CoC PDF + passenger charter PDF, but charter mostly duplicates CAR text).
- Team split: pending.

## 7. IndiGo CoC findings (Oct 20 2026, goindigo.in/information/conditions-of-carriage.html)
- Conflict 1: IndiGo Force Majeure includes manpower/parts shortage and mechanical/technical/operational failures; CAR 1.4/1.5 don't list technical faults -> escalate PROVISION_CONFLICT on "technical snag" claims.
- Conflict 2: CoC 10.2(c) pays denied-boarding compensation as non-encashable vouchers; CAR 3.7.1 allows vouchers only with signed passenger agreement.
- Agreements: contact-info exclusion (CAR 3.3.3); denied boarding 1h rule (3.2.2) and connecting 3h rule (3.2.3) copied in 10.2.
- CoC has a Vietnam-specific section (Decree 208/2026/ND-CP) -> out-of-jurisdiction escalation test.

# Operational runbooks

These steps use the services and commands in the current Compose stack. Run Docker
commands from the repository root. Do not put provider keys in command arguments or
logs.

## LLM provider unavailable or circuit open

**Symptom:** A run ends in `escalated` with a provider error, the worker logs failed
requests, or the local LLM stub is unhealthy.

**Confirm:**

```bash
docker compose ps worker llm-stub
docker compose logs --tail=100 worker llm-stub
curl -fsS http://localhost:8001/healthz
```

The HTTP check only confirms the local stub. For OpenRouter, inspect the worker logs
and verify the non-secret settings inside the container:

```bash
docker compose exec worker python -c "from triage_common.settings import settings; print('provider=', settings.llm_provider, 'primary_model_set=', bool(settings.llm_model_primary), 'fallback_model_set=', bool(settings.llm_model_fallback), 'api_key_set=', bool(settings.openrouter_api_key))"
```

**Fix:** With the default `LLM_PROVIDER=stub`, restart the stub and worker:

```bash
docker compose restart llm-stub worker
```

For `LLM_PROVIDER=openrouter`, correct the provider/model/key values in the local
`.env` without sharing the key, then recreate the worker so it reloads the settings:

```bash
docker compose up -d --force-recreate worker
```

**Verify:** Confirm `llm-stub` is healthy when using the stub and that worker logs no
longer report provider failures. Submit a demo claim in the web client or with the
README curl example and poll its run until it reaches a terminal status.

## Chroma is unhealthy or the collection appears empty

**Symptom:** Chroma is unhealthy, worker logs report connection failures, or a
retrieval operation returns no passages.

**Confirm:**

```bash
docker compose ps chroma worker ingest
docker compose logs --tail=100 chroma worker ingest
```

Compose's Chroma health check calls its heartbeat endpoint. A healthy heartbeat
confirms the server is reachable; it does **not** confirm that a collection is
populated.

**Fix:** If Chroma is running but unhealthy, restart it:

```bash
docker compose restart chroma
```

If it is stopped, start it instead:

```bash
docker compose up -d chroma
```

Then inspect its logs:

```bash
docker compose logs --tail=100 chroma
```

If the collection is empty or stale, rerun the serialized ingest job:

```bash
docker compose run --rm ingest
```

It verifies the pinned sources, builds the sidecars, synchronizes `CHROMA_COLLECTION`
(default `triage-clauses-v1`), and checks exact contents plus semantic retrieval.
Unchanged records are skipped; changed records are upserted before obsolete IDs are
deleted. Do not run multiple ingest jobs concurrently. The collection is owned by
this job; unrelated records in it will be removed. Partial failures can be repaired
by rerunning the job. The sync is not a transactional snapshot, so ingest before
starting consumers or pause consumers while changing the corpus.

**Verify:** Run the read-only collection check, rather than relying on heartbeat:

```bash
docker compose run --rm --no-deps ingest python cli/index_corpus.py --verify
```

The pinned corpus must contain exactly 246 IDs with matching text, provenance and
384-dimensional embeddings. The smoke query must retrieve
`CAR-S3-M-IV-R4:3.8.1(a):0001` among its first five results. A missing collection or
mismatch exits nonzero. On an unchanged re-ingest, expect `upserted: 0`, `deleted: 0`.
If stored content or vectors were altered independently, repair them with
`docker compose run --rm --no-deps ingest python cli/index_corpus.py --force`, then
repeat the read-only check.
The first embedding/query downloads MiniLM weights; the `embedding_cache` volume
retains them for later ingest containers. Download failures require network access
and a rerun. Changing embedding models requires an explicit implementation and a
new collection name; incompatible collection metadata fails rather than mixing vectors.

The agent graph still uses issue #6's demo fixture search. This check verifies the
real corpus index independently; it does not imply the graph uses authoritative retrieval.

## Redis queue backlog is growing

**Symptom:** Claims remain `queued`, or the number of waiting jobs keeps increasing.

**Confirm:**

```bash
docker compose ps api worker redis
docker compose logs --tail=100 worker redis
docker compose exec redis redis-cli LLEN triage:runs
```

The queue command reports waiting jobs; a job currently being processed is not in
that list.

**Fix:** Choose the recovery action that matches the confirmed cause. If Redis is
unhealthy, restart Redis and wait for its health check before taking further action:

```bash
docker compose restart redis
docker compose ps redis
```

If Redis is healthy and workers are saturated, add worker capacity without restarting
the existing workers:

```bash
docker compose up -d --scale worker=3
```

If a worker has exited, inspect the affected run IDs before restarting it. The worker
removes a job from Redis before processing and does not requeue it after a crash, so
restarting can leave that run stuck and will not recover its job automatically.
Restart only the stopped worker after recording affected runs:

```bash
docker compose restart worker
```

Do not delete or flush the Redis queue as a backlog fix.

**Verify:** Confirm Redis and workers are healthy, `LLEN triage:runs` is trending
down, and queued runs reach `done`, `escalated`, or `failed` when polled through
the API.

## Verifier worker crash

**Symptom:** Verification is unavailable or a run fails during agent processing.

**Confirm:**

```bash
docker compose ps worker
docker compose logs --tail=100 worker
```

There is currently no separate `verifier-worker` Compose service. Verification is a
pass-through TODO in the agent graph, so a verifier-specific container cannot be
restarted or checked. **TODO: implement the verifier and its operational checks in
issue #6.**

**Fix:** If the existing `worker` process itself has stopped, restart it:

```bash
docker compose restart worker
```

The current consumer removes a job from Redis before processing it and does not
acknowledge/requeue a job after a crash. Inspect affected run IDs through the API;
do not assume a run interrupted by a worker crash will be retried automatically.

**Verify:** Confirm the `worker` container is running and its logs show `worker
started`. Submit a new demo claim and poll it to a terminal status. The new claim
verifies the current worker path; it does not provide the missing independent
verifier guarantee.

## Re-ingest the source corpus

**Symptom:** A source document is missing or fails its manifest hash check, or the
generated clause files need to be rebuilt.

**Confirm:**

```bash
docker compose ps -a ingest
docker compose logs --tail=100 ingest
```

The one-shot Compose service fetches and verifies source documents, writes
`chunks.jsonl`, `refs.json`, and `definitions.json` under `corpus/processed/`, then
synchronizes Chroma and verifies its contents and known retrieval.

**Fix:** From the repository root, rerun the same one-shot job:

```bash
docker compose run --rm ingest
```

For local development outside Compose, the equivalent source and chunk steps are:

```bash
make corpus
make ingest
CHROMA_URL=http://localhost:<published-chroma-port> make index
CHROMA_URL=http://localhost:<published-chroma-port> make index-check
```

If a hash mismatch persists, stop and compare the source with `corpus/manifest.yaml`;
do not change the expected hash just to bypass verification. The default Compose job also populates Chroma (see the Chroma runbook). Chroma is
internal-only in Compose; the local index commands require a reachable Chroma URL.

**Verify:** The job exits successfully and reports its chunk count. In Compose,
`docker compose ps -a ingest` should show `Exited (0)`; locally, confirm the three
processed files exist. The final JSON reports the collection count and retrieved IDs; failures in indexing
or retrieval make the ingest job fail and block worker startup.

## Grafana dashboards or Prometheus metrics are missing

**Confirm:** Open http://localhost:3001 and inspect the **Claim Triage** folder. It
contains **Triage — Services** (`triage-services`) and **Triage — Agents**
(`triage-agents`). Sign in using the Grafana credentials from your local `.env`.
Check http://localhost:9090/targets: `api`, every `worker` replica, and `prometheus`
should be up. Worker metrics listen internally on port 8002. Keep
`WORKER_METRICS_PORT` consistent with Prometheus's discovery port if changing it.

Service panels show accepted/quarantined throughput, submit p95 latency, scrape
availability, process memory/CPU, and scraped samples. Agent panels show starts,
outcomes by route/reason, active runs, run/node latency, steps, tool reservations,
and escalation fraction. Run IDs and correlation IDs are trace attributes, never
metric labels. There is no container exporter, cost, token, retry or independent
verification instrumentation yet; these dashboards do not invent those series.
The fixture graph currently escalates every run, so escalation is expected.

**Fix:** Inspect logs and restore the checked-in provisioning mounts/configuration:

```bash
docker compose logs --tail=100 prometheus grafana worker
docker compose restart prometheus grafana
```

If worker metrics are absent, rebuild/recreate the worker. Prometheus DNS discovery
finds each scaled replica within approximately 10 seconds; allow another scrape
interval for metrics. A single run may yield zero rates after its first scrape;
submit several runs over multiple scrapes to populate rate/latency panels.

**Verify:** Submit a claim, confirm `agent_runs_started_total` increases in
Prometheus, and check the provisioned dashboards. A useful discovery query is
`up{job="worker"}`: expect one series per live worker replica.

## API/worker traces are missing from Jaeger

**Confirm:** Both services configure OTLP/gRPC exporters using
`OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://jaeger:4317`). Jaeger explicitly enables
its OTLP receiver. API publish spans inject W3C context into Redis messages; workers
extract it and emit consumer, graph-node and fixture-search spans. Claims and provider
keys are not added to span attributes. Application spans carry `run_id` and
`correlation_id` on the queue hop.

**Fix:** Check the exporter endpoint and collector, then recreate services if `.env`
changed:

```bash
docker compose logs --tail=100 api worker jaeger
docker compose restart jaeger
docker compose up -d --build api worker
```

**Verify:** Run the repeatable check (submits one demo claim):

```bash
uv run python scripts/smoke_observability.py
```

It waits for a terminal run and for a single Jaeger trace containing both
`triage-api` and `triage-worker`, publish/consume spans, all four graph nodes and
`tool.search_corpus`. It prints the run ID and a direct Jaeger trace URL; missing
spans fail the check. Alternatively search http://localhost:16686 for service
`triage-worker` and tag `run_id=<UUID>`. Batch export may take several seconds.
Older jobs without a trace carrier start a new worker trace; submit a new claim
to test cross-service correlation. Jaeger all-in-one uses in-memory trace storage,
so a collector restart discards old traces. `/v1/runs/{run_id}/trace` remains the
persisted application step log, independently of Jaeger retention.

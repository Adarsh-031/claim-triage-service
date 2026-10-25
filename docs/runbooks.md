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

If the collection is empty, there is no supported indexing repair command yet.
`make ingest` creates clause chunks and sidecar files under `corpus/processed/`; it
does not embed or write those chunks to Chroma. **TODO: implement Chroma indexing and
retrieval verification in issue #21.** Do not treat a healthy heartbeat or successful
chunk generation as proof that retrieval is ready.

**Verify:** For an availability problem, `docker compose ps chroma` should show
`healthy`, and worker logs should stop reporting Chroma connection errors. For an
empty collection, verification remains blocked until the indexing TODO is implemented.

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
down, and queued runs reach `completed`, `escalated`, or `failed` when polled through
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

The one-shot Compose service fetches and verifies source documents, then writes
`chunks.jsonl`, `refs.json`, and `definitions.json` under `corpus/processed/`.

**Fix:** From the repository root, rerun the same one-shot job:

```bash
docker compose run --rm ingest
```

For local development outside Compose, the equivalent source and chunk steps are:

```bash
make corpus
make ingest
```

If a hash mismatch persists, stop and compare the source with `corpus/manifest.yaml`;
do not change the expected hash just to bypass verification. Re-ingestion currently
rebuilds chunk files only and does not populate Chroma (see the Chroma runbook and
issue #21).

**Verify:** The job exits successfully and reports its chunk count. In Compose,
`docker compose ps -a ingest` should show `Exited (0)`; locally, confirm the three
processed files exist. This verifies corpus parsing/export, not vector indexing.

## Observability tooling limits

- **TODO, issue #21:** Agent-level Grafana dashboards are not provisioned. The Grafana
  container being healthy does not mean the planned agent dashboard exists.
- **TODO, issue #21:** Jaeger is present in Compose, but the current API/worker code
  does not emit OpenTelemetry spans. Use worker logs and
  `GET /v1/runs/{run_id}/trace` for the currently implemented run trace; the Jaeger UI
  being available does not confirm application traces are arriving.

Issue #6 tracks the agent graph and pass-through verifier only. Issue #21 tracks Chroma
indexing, Grafana dashboards, and Jaeger instrumentation.

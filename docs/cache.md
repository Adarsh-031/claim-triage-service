# Cache and request limits

`CACHE_ENABLED=true` enables embedding reuse and whole-run caching; setting it to
`false` bypasses both. `CACHE_TTL_S` defaults to one hour. Cache keys are SHA-256
digests and do not expose claim text or client identifiers.

- **Embedding vectors:** the Chroma indexer reuses a stored vector when the full
  contextual embedding input is unchanged. The key covers document ID, heading path,
  and chunk text; a changed embedding input is recomputed. Metadata-only changes still
  update the index record.
- **Whole runs:** keys include normalized claim, prompt version, corpus version, and
  `RUN_CACHE_VERSION` (`graph-v1`),
  scoped to the requesting client so one client cannot receive another client's
  cached response. Successful results are cached after the worker persists them.

After a successful corpus index sync, `chunks.jsonl`, `refs.json`, and
`definitions.json` are fingerprinted with the same version function used by the
worker, and that version is published in Redis. A change to any processed artifact
invalidates cached runs. Increment `RUN_CACHE_VERSION` whenever graph behavior
changes so deployed fixes cannot reuse old answers. Invalid or unreadable cache
entries are deleted and treated as misses; Redis is required for request limiting
and the queue.

The API uses a Redis token bucket with capacity `RATE_LIMIT_PER_MIN` (10 by default)
and refills that many tokens per minute per source IP; caller-provided `X-Client-ID`
is ignored. A rejected request returns 429 with `Retry-After` in seconds. If Redis
cannot evaluate the limiter, the API returns 503 and increments
`rate_limit_redis_errors_total`. `api_cache_hits_total` and
`worker_cache_hits_total` expose cache hits, and `submit_latency_seconds` records API
submission latency for p95 comparison. CI starts a pinned Redis service and tests
the Lua token bucket against it, including concurrent requests, refill, and expiry.

## Measuring cache impact

Run the same repeatable scenario set against two otherwise identical deployments,
with `CACHE_ENABLED=true` and `CACHE_ENABLED=false`, using the same Redis data and
prompt/corpus versions. Query the API histogram for p95 submit latency and compare
cache-hit counters. Record the exact model, provider-reported token usage, model price
schedule, corpus manifest hash, and prompt version alongside the result. The current
deterministic graph has no billable LLM usage, so monetary savings are not reported
until a provider-backed graph and pricing data are configured; cache-hit count alone
must not be presented as currency savings. For load tests such as issue #34, set
`RATE_LIMIT_PER_MIN` above the test's per-IP request volume to avoid testing the
limiter instead of worker capacity.

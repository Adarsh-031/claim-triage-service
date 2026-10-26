# Cache and request limits

`CACHE_ENABLED=true` enables embedding reuse, retrieval caching, and whole-run
caching; setting it to `false` bypasses all three. `CACHE_TTL_S` defaults to one hour. Cache keys are SHA-256
digests and do not expose claim text or client identifiers.

- **Embedding vectors:** the Chroma indexer reuses a stored vector when the full
  contextual embedding input is unchanged. The key covers document ID, heading path,
  and chunk text; a changed embedding input is recomputed. Metadata-only changes still
  update the index record.
- **Retrieval:** keys include normalized query, retrieval mode, result count, and the
  current corpus version.
- **Whole runs:** keys include normalized claim, prompt version, and corpus version,
  scoped to the requesting client so one client cannot receive another client's
  cached response. Successful results are cached after the worker persists them.

After a successful corpus index sync, `manifest.yaml` is hashed and the version is
published in Redis. A changed manifest therefore produces fresh retrieval and run
keys. Cache read/write failures are treated as misses; Redis is still required for
request limiting and the queue.

The API uses a Redis token bucket with capacity `RATE_LIMIT_PER_MIN` (10 by default)
and refills that many tokens per minute for each `X-Client-ID` or source IP. A rejected
request returns 429 with `Retry-After` in seconds. `api_cache_hits_total` and
`worker_cache_hits_total` expose cache hits, and `submit_latency_seconds` records API
submission latency for p95 comparison.

## Measuring cache impact

Run the same repeatable scenario set against two otherwise identical deployments,
with `CACHE_ENABLED=true` and `CACHE_ENABLED=false`, using the same Redis data and
prompt/corpus versions. Query the API histogram for p95 submit latency and compare
cache-hit counters. Record the exact model, provider-reported token usage, model price
schedule, corpus manifest hash, and prompt version alongside the result. The current
deterministic graph has no billable LLM usage, so monetary savings are not reported
until a provider-backed graph and pricing data are configured; cache-hit count alone
must not be presented as currency savings.

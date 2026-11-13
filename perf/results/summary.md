# Local load-test check — 13 November 2026

Ran the existing Locust workload against the local Docker Compose stack, with 10
users, a spawn rate of 2 users/second, and a 120-second load-generation window per
test. In-flight tasks were allowed to finish before each test exited. The LLM
provider was the stub, result caching was disabled, the rate limit was 100,000 per
minute, and the verifier remained at one replica. Redis was not flushed.

- **One agent worker:** 27 completed claims; zero HTTP or E2E failures; submit p95
  28 ms; E2E p95 approximately **87 seconds**. Outcomes: 23 done, 4 escalated.
  Test ran 05:11:09–05:13:46 UTC (10:41:09–10:43:46 IST).
- **Three agent workers:** 66 completed claims; zero HTTP or E2E failures; submit
  p95 29 ms; E2E p95 approximately **25 seconds**. Outcomes: 61 done, 5 escalated.
  Test ran 05:14:33–05:16:46 UTC (10:44:33–10:46:46 IST).

The one-worker test exceeded the project's 25-second E2E p95 target. The
three-worker result was at the target boundary using Locust's rounded percentile
estimate. This was a short check with randomized scenarios and a small sample,
not the full concurrency sweep or a sustained-capacity certification. Final
counts and percentiles above come from Locust's shutdown summaries; the periodic
CSV snapshots can lag the final completed task. Terminal outcome counts were
independently checked in PostgreSQL. Escalation is a terminal application outcome,
not a load-test failure.

## Observability checks and repair

- Grafana authentication, its database health, the provisioned dashboards, and the
  Prometheus datasource health check passed. Dashboard PromQL expressions were
  also executed through Grafana's datasource proxy.
- Prometheus scraped all three agent-worker replicas, the API, the verifier,
  cAdvisor, and itself successfully during the three-worker test.
- Container CPU/memory panels initially had no per-service data. cAdvisor
  v0.49.1 could not resolve Docker 29.9.3's containerd snapshotter layer paths.
  Updated `docker-compose.yml` to the official
  `ghcr.io/google/cadvisor:v0.60.6` image and recreated only cAdvisor. Both panel
  queries subsequently returned finite values for 13 Compose services.
  Containerd snapshotter support was introduced in the
  [upstream v0.54.0 release](https://github.com/google/cadvisor/releases/tag/v0.54.0).
- Guardrail-block and provider retry/fallback series were absent for events that
  were not exercised by this workload; this is distinct from the repaired
  missing container metrics.
- Jaeger contained a correlated completed trace spanning `triage-api`,
  `triage-worker`, and `triage-verifier`:
  [open the verified trace](http://localhost:16686/trace/064a00904348563f9c911217beefd25e).
- API readiness passed after testing. All run/verify queues and processing lists
  were empty. Load generation stopped and the original single-agent-worker
  replica count was restored; the existing load-test environment overrides were
  left as they were before testing.

Browser automation was unavailable, so dashboard verification used Grafana's
authenticated API and actual datasource queries, rather than screenshots.

## Open the recorded time window

- [Triage — Agents](http://localhost:3001/d/triage-agents?from=1794546600000&to=1794547200000)
- [Triage — Services](http://localhost:3001/d/triage-services?from=1794546600000&to=1794547200000)

The fixed window is 05:10–05:20 UTC (10:40–10:50 IST). Per-container resource data
starts after the cAdvisor repair near the end of the second test; it is not
available for the earlier one-worker test. Prometheus uses five-minute windows in
most rate panels, so adjacent tests can appear smoothed together. Use Locust E2E
measurements above for the latency comparison. Grafana's run-duration metric
measures worker execution and excludes the complete client queue wait.

Raw CSV files and observability query snapshots are alongside this report, with
the prefix `check-20261113-`. These raw files are ignored by Git. The automated
`scripts/loadtest.sh` harness overwrites this summary when it is run.

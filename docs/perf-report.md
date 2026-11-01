# Performance report (issue #34)

Status: **harness ready, results not yet measured.** Run `scripts/loadtest.sh` on the
demo machine. It writes `perf/results/summary.md`; paste that table below and add the two
Grafana screenshots. Nothing in the results section is invented.

## SLO

End-to-end p95 ≤ 25 s at 10 concurrent claims (docs/02-design.md), with submit p95
measured separately, since the API only enqueues.

## Setup

- Provider: `llm-stub` at `STUB_DELAY=1.5` s per call. The override file forces
  `LLM_PROVIDER=stub`, so no OpenRouter quota is spent.
- Result cache off, rate limit lifted (all Locust users share one IP), unique PNR per claim.
- Workload: every valid eval scenario at random (no quarantine or guardrail cases).
- Each Locust user submits a claim, polls `GET /v1/runs/{id}` every 0.5 s until it is
  terminal, waits 0.5 to 1.5 s, then repeats. So *N users = N claims in flight*.
- Steps: 1, 5, 10, 15, 20 users, 90 s each, with 1 and then 3 `worker` replicas
  (`verifier-worker` stays at 1).

```bash
scripts/loadtest.sh            # both runs; about 16 minutes
# or interactively: http://localhost:8089 after
docker compose -f docker-compose.yml -f deploy/locust/compose.loadtest.yml --profile loadtest up -d --build --wait
```

## Results

_Paste `perf/results/summary.md` here._

| Workers | Users | Claims | Failed | Claims/s | Submit p95 | E2E p50 | E2E p95 | SLO |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| 1 | … | | | | | | | |
| 3 | … | | | | | | | |

Screenshots: `docs/img/perf-w1.png`, `docs/img/perf-w3.png` (Grafana **Triage — Agents**:
active runs, run duration p95, LLM calls/run; **Triage — Services**: container CPU/memory).

## What to look for, and how to name the bottleneck

The model the numbers should be checked against:

- One agent-worker processes **one run at a time** (`consume_once` loop, no concurrency).
  A run makes *k* sequential stub calls, so its service time is *S ≈ 1.5 s × k* plus
  small overhead. *k* is in each run's trace: `usage.llm_calls`.
- Throughput ceiling with *W* workers is about *W / S* claims/s. Above that, claims wait in
  `triage:runs`, and end-to-end latency grows by roughly *queue length × S / W*.
- With 10 claims in flight and 1 worker, the last claim waits for about 9 others, so
  p95 ≈ 10 × S. The SLO breaks once S > 2.5 s, which is about two stub calls per run.
  Three workers should cut queueing about 3×, until the next stage saturates.
- That next stage is the **single verifier-worker**: it does one verification at a time
  (one LLM call, so about 1.5 s). Once agent-workers outrun it, `triage:verify` grows and
  scaling `worker` further stops helping. Check this in Grafana: active runs flat on
  workers while runs pile up in `verifying`.
- The stub, API, Postgres and Redis should stay far from saturation. Submit p95 should
  stay in the tens of milliseconds, and container CPU stay low. If submit p95 climbs,
  the API's synchronous DB and Redis calls are the bottleneck instead.

Conclusion (fill from data): _the SLO first breaks at N users with 1 worker and M with 3;
the bottleneck is …, because …_. The fix that follows is to scale `verifier-worker`, or
run several runs concurrently per worker, since the work is I/O-bound waiting on the LLM.

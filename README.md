# Air Passenger Compensation Claim Triage Service

Multi-agent service that triages Indian domestic air-passenger disruption claims against
**DGCA CAR Section 3, Series M, Part IV (Rev 4, effective 15 Feb 2023)** and one airline's
Conditions of Carriage. It recommends `pay_as_computed`, `request_further_evidence` or `decline`
with clause-level citations, or escalates to a human. **It never authorises payment.**

Course: UE24AM342AA5 ML System Design & AgentOps, Mini Project 04 (PES University).

![architecture](docs/img/architecture.png)

## Quick start

Local dev uses [uv](https://docs.astral.sh/uv/) (one workspace, one `uv.lock`):

```bash
uv sync            # creates .venv with every service + dev tools
uv run pytest      # tests
uv add --package triage-worker <pkg>   # add a dependency to one service
```

Build the clause corpus (requires Poppler's `pdftotext`, e.g. `sudo apt install poppler-utils`):

```bash
make corpus        # download/verify the manifest-pinned sources
make ingest        # write corpus/processed/{chunks.jsonl,refs.json,definitions.json}
```

See [the ingestion guide](docs/ingestion.md) for artifact formats, reference resolution, and tests.

Run the stack:

```bash
cp .env.example .env          # defaults use the offline LLM stub
docker compose up --build
```

| Service | URL |
|---|---|
| Web client | http://localhost:3000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Jaeger | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (admin / admin) |

Submit a claim:

```bash
curl -s -X POST localhost:8000/v1/claims -H 'content-type: application/json' \
  -d @eval/scenarios/s01_demo_cancellation_alternate_taken.json | jq
curl -s localhost:8000/v1/runs/<run_id> | jq
```

Scale workers: `docker compose up --scale worker=3`

## Layout

```
services/api       FastAPI: contract validation, quarantine, run API, health, metrics
services/worker    Queue consumer + LangGraph agent, tools, LLM provider interface
services/web       Static officer console (A8: appearance not assessed)
libs/common        Shared data contracts (Pydantic) and settings
cli                ingest.py (idempotent corpus ingestion), evaluate.py (eval set)
corpus/            Source documents + manifest (versions, URLs, hashes)
eval/scenarios     >=20 fixed scenarios with expected outcome and passage
deploy/            Prometheus, Grafana provisioning
docs/              Requirements, design, report material, runbooks, incident log
```

## Status

| Priority | Item | State |
|---|---|---|
| P0 | Compose, API, queue, worker, run polling | scaffolded |
| P0 | Clause chunks + reference/definition exports | implemented; embedding/indexing pending |
| P0 | Agent with retrieval tools, demo case 4.1 | TODO |
| P1 | Verifier, escalation, observability, eval, circuit breaker, web client | partial |
| P2 | Dashboards, Locust/scaling study, caching study, prompt A/B, runbooks | TODO |

## Configuration

All model IDs, endpoints and keys come from `.env` (never committed). `LLM_PROVIDER=stub`
runs fully offline and is used for development, evaluation and load tests.

# Observability (L5, issue #33)

## One trace across three services

`api` → Redis `triage:runs` → `agent-worker` → Redis `triage:verify` → `verifier-worker`.
Each hop carries W3C `trace_context` in the queue message, so Jaeger
(http://localhost:16686) shows one trace with spans from `triage-api`, `triage-worker`
and `triage-verifier`:

`queue.publish` → `queue.process` → `agent.intake` → `llm.chat` → `agent.research` →
`research.llm` / `llm.chat` / `tool.<name>` → `agent.draft` → `verify.publish` →
`verify.process` → `agent.verify` → `verify.checks` / `llm.chat` → `agent.finalize`.

`llm.chat` spans carry `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `llm.cost_usd` and `run.step`. `agent.finalize` carries
`run.cost_usd`, `run.tokens` and `run.llm_calls`.

## Tokens and cost, per call, per step, per run

`worker/usage.py` records every provider call at the time of the call:
`{step, model, outcome, prompt_tokens, completion_tokens, cost_usd, latency_ms}`. The
`step` is the graph node the call ran in. Cost is OpenRouter's reported `usage.cost`
(requested with `usage: {include: true}`); otherwise `LLM_PRICE_PROMPT_PER_MTOK` /
`LLM_PRICE_COMPLETION_PER_MTOK` × tokens. Free models report 0.

The list rides in graph state across the verifier hand-off; finalize sums it.

```bash
curl -s localhost:8000/v1/runs/$RUN/trace | jq '{usage, llm_calls}'
```

## Metrics (Prometheus :9090, Grafana :3001)

| Metric | Meaning |
|---|---|
| `agent_run_cost_usd`, `agent_run_tokens`, `agent_run_llm_calls` | per-run histograms |
| `llm_calls_total{model,outcome}`, `llm_tokens_total{model,kind}`, `llm_cost_usd_total{model}` | per-call counters |
| `llm_call_duration_seconds{model}` | provider latency |
| `llm_retries_total`, `llm_fallbacks_total`, `agent_verifier_retries_total` | retries |
| `llm_circuit_breaker_state{provider}` | 0 closed, 1 open, 2 half-open |
| `agent_steps`, `agent_tool_calls` | steps and tool calls per run |
| `guardrail_blocks_total{stage,kind,rule}` | guardrail blocks (#32) |
| `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes` | cAdvisor, per Compose service |

Dashboards: **Triage — Agents** (cost/tokens/calls per run, latency, retries, breaker)
and **Triage — Services** (container CPU and memory by service, guardrail blocks).
cAdvisor UI: http://localhost:8088.

## Logs

Every service writes one JSON object per line with `service`, `run_id`, `trace_id` and
`span_id`, so a run can be followed with:

```bash
docker compose logs api worker verifier-worker | grep <run_id>
```

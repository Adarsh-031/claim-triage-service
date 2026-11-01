"""Bounded-label worker metrics: run IDs belong in traces, never metric labels."""
from prometheus_client import Counter, Gauge, Histogram

RUNS_STARTED = Counter("agent_runs_started_total", "Runs started by workers")
RUNS = Counter("agent_runs_total", "Finished runs", ["route", "reason"])
ACTIVE = Gauge("agent_runs_active", "Currently executing runs")
RUN_LATENCY = Histogram("agent_run_duration_seconds", "Worker execution time",
                        buckets=(.01, .1, .5, 1, 5, 10, 30, 60, 120))
STEPS = Histogram("agent_steps", "Work steps per run", buckets=(0, 1, 2, 3, 5, 8, 12, 20))
TOOLS = Histogram("agent_tool_calls", "Tool reservations per run", buckets=(0, 1, 2, 5, 10, 15, 20))
NODE_LATENCY = Histogram("agent_node_duration_seconds", "Graph node duration", ["node"])
CACHE_HITS = Counter("worker_cache_hits_total", "Worker cache hits by component", ["component"])
LLM_BREAKER_STATE = Gauge(
    "llm_circuit_breaker_state",
    "LLM provider breaker state: 0=closed, 1=open, 2=half-open",
    ["provider"],
)

# LLM usage (#33). Model names come from configuration, so the label set stays bounded.
LLM_CALLS = Counter("llm_calls_total", "Provider calls", ["model", "outcome"])
LLM_TOKENS = Counter("llm_tokens_total", "Tokens by model and kind", ["model", "kind"])
LLM_COST = Counter("llm_cost_usd_total", "LLM cost in USD", ["model"])
LLM_LATENCY = Histogram("llm_call_duration_seconds", "Provider call latency", ["model"],
                        buckets=(.1, .5, 1, 2, 5, 10, 20, 45))
LLM_RETRIES = Counter("llm_retries_total", "Primary-model retries after a failure", ["provider"])
LLM_FALLBACKS = Counter("llm_fallbacks_total", "Calls served by the fallback model", ["provider"])
RUN_TOKENS = Histogram("agent_run_tokens", "Prompt + completion tokens per run",
                       buckets=(0, 500, 1000, 2000, 5000, 10000, 20000, 50000))
RUN_COST = Histogram("agent_run_cost_usd", "LLM cost per run in USD",
                     buckets=(0, 0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5))
RUN_LLM_CALLS = Histogram("agent_run_llm_calls", "Provider calls per run", buckets=(0, 1, 2, 3, 5, 8, 12, 20))
VERIFIER_RETRIES = Counter("agent_verifier_retries_total", "Verify -> research retry rounds")

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

"""Per-call LLM usage: tokens and cost recorded at the time of the call (L5, issue #33).

Graph nodes open ``collect(state, node)``; every provider call made inside it appends one
record to ``state["llm_calls"]`` tagged with that node, so cost is attributed per step and,
summed in finalize, per run. The list rides in graph state, so it survives the
agent-worker -> verifier-worker hand-off.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from triage_common.settings import settings

from .metrics import LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS

_SINK: ContextVar[list | None] = ContextVar("llm_usage_sink", default=None)
_STEP: ContextVar[str] = ContextVar("llm_usage_step", default="unattributed")


def cost_usd(model: str, usage: dict) -> float:
    """Provider-reported cost when given (OpenRouter ``usage.cost``), else the configured price.

    Free models cost 0; ``LLM_PRICE_*_PER_MTOK`` lets a paid model be priced without code.
    """
    if isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    return (int(usage.get("prompt_tokens") or 0) * settings.llm_price_prompt_per_mtok
            + int(usage.get("completion_tokens") or 0) * settings.llm_price_completion_per_mtok) / 1e6


def record(model: str, usage: dict | None, latency_s: float, outcome: str = "ok") -> dict:
    usage = usage or {}
    entry = {
        "step": _STEP.get(), "model": model, "outcome": outcome,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cost_usd": round(cost_usd(model, usage), 8), "latency_ms": round(latency_s * 1000),
    }
    LLM_CALLS.labels(model, outcome).inc()
    LLM_LATENCY.labels(model).observe(latency_s)
    LLM_TOKENS.labels(model, "prompt").inc(entry["prompt_tokens"])
    LLM_TOKENS.labels(model, "completion").inc(entry["completion_tokens"])
    LLM_COST.labels(model).inc(entry["cost_usd"])
    if (sink := _SINK.get()) is not None:
        sink.append(entry)
    return entry


@contextmanager
def collect(state: dict, step: str) -> Iterator[list[dict]]:
    """Yield the run's call list; the caller stores it back on the state the node returns."""
    calls = list(state.get("llm_calls") or [])
    sink_token, step_token = _SINK.set(calls), _STEP.set(step)
    try:
        yield calls
    finally:
        _SINK.reset(sink_token)
        _STEP.reset(step_token)


def totals(calls: list[dict]) -> dict:
    by_step: dict[str, dict] = {}
    for c in calls:
        s = by_step.setdefault(c["step"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        s["calls"] += 1
        s["prompt_tokens"] += c["prompt_tokens"]
        s["completion_tokens"] += c["completion_tokens"]
        s["cost_usd"] = round(s["cost_usd"] + c["cost_usd"], 8)
    return {
        "llm_calls": len(calls),
        "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
        "completion_tokens": sum(c["completion_tokens"] for c in calls),
        "cost_usd": round(sum(c["cost_usd"] for c in calls), 8),
        "by_step": by_step,
    }

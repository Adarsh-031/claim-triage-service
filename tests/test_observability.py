"""Issue #33: per-call token/cost attribution, run totals in the trace, JSON logs."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from triage_common.logs import JsonFormatter, log_context
from worker import usage
from worker.agent import graph
from worker.agent.llm import OpenAICompatibleLLM

from tests.test_verifier import S05


def test_cost_prefers_provider_reported_cost(monkeypatch):
    assert usage.cost_usd("m", {"cost": 0.0123, "prompt_tokens": 10}) == 0.0123
    monkeypatch.setattr(usage.settings, "llm_price_prompt_per_mtok", 1.0)
    monkeypatch.setattr(usage.settings, "llm_price_completion_per_mtok", 2.0)
    assert usage.cost_usd("m", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}) == pytest.approx(2.0)


def test_calls_are_attributed_to_the_node_they_ran_in():
    state = {}
    with usage.collect(state, "research") as calls:
        usage.record("m", {"prompt_tokens": 100, "completion_tokens": 20}, 0.5)
    state["llm_calls"] = calls
    with usage.collect(state, "verify") as calls:
        usage.record("m", {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.001}, 0.2)
    totals = usage.totals(calls)
    assert totals["llm_calls"] == 2 and totals["prompt_tokens"] == 150
    assert set(totals["by_step"]) == {"research", "verify"}
    assert totals["by_step"]["verify"]["cost_usd"] == 0.001


def test_http_client_records_usage_and_failures():
    def handler(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"] == "fail":
            return httpx.Response(500)
        assert body["usage"] == {"include": True}
        return httpx.Response(200, json={"model": "m-real", "choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.0002}})

    llm = OpenAICompatibleLLM("http://x/v1", "m", "k", report_cost=True)
    llm.client = httpx.Client(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    state = {}
    with usage.collect(state, "intake") as calls:
        llm.chat([{"role": "user", "content": "hi"}])
        with pytest.raises(httpx.HTTPStatusError):
            llm.chat([{"role": "user", "content": "fail"}])
    assert calls[0] | {"latency_ms": 0} == {"step": "intake", "model": "m-real", "outcome": "ok", "prompt_tokens": 7,
                                             "completion_tokens": 3, "cost_usd": 0.0002, "latency_ms": 0}
    assert calls[1]["outcome"] == "HTTPStatusError" and calls[1]["prompt_tokens"] == 0


@pytest.mark.usefixtures("research_env")
def test_finalize_trace_carries_run_usage_per_step():
    result, steps = graph.run_agent(S05["claim"])
    final = next(s for s in steps if s["step"] == "finalize")
    assert final["usage"]["llm_calls"] == len(final["llm_calls"]) > 0
    assert {c["step"] for c in final["llm_calls"]} <= {"intake", "research", "draft", "verify", "retry"}
    assert "research" in final["usage"]["by_step"]


def test_json_logs_carry_run_and_trace_ids():
    provider = TracerProvider()
    formatter = JsonFormatter("agent-worker")
    record = logging.LogRecord("worker", logging.INFO, __file__, 1, "claimed %s", ("job",), None)
    with log_context("run-123"), provider.get_tracer("t").start_as_current_span("queue.process"):
        line = json.loads(formatter.format(record))
        expected_trace = format(trace.get_current_span().get_span_context().trace_id, "032x")
    assert line["run_id"] == "run-123" and line["service"] == "agent-worker"
    assert line["msg"] == "claimed job" and line["trace_id"] == expected_trace

"""Queue consumer. One process = one agent worker; scale with `--scale worker=N` (A7)."""
from __future__ import annotations

import json
import logging
import uuid

import redis
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import start_http_server
from triage_common.contracts import EscalationReason, Recommendation, Route, RunStatus
from triage_common.settings import settings
from triage_common.store import get_run, init_db, update_run
from triage_common.telemetry import configure_tracing

from .agent.graph import run_agent
from .metrics import ACTIVE, RUN_LATENCY, RUNS, RUNS_STARTED, STEPS, TOOLS

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("worker")


def handle(job: dict) -> None:
    with otel_trace.get_tracer(__name__).start_as_current_span(
        "queue.process", context=extract(job.get("trace_context") or {}), kind=SpanKind.CONSUMER,
        attributes={"run_id": job["run_id"], "correlation_id": job.get("cid", ""),
                    "messaging.system": "redis", "messaging.destination.name": settings.queue_name},
    ), ACTIVE.track_inprogress(), RUN_LATENCY.time():
        _handle(job)


def _handle(job: dict) -> None:
    run_id = uuid.UUID(job["run_id"])
    run = get_run(run_id)
    if run is None:
        return
    RUNS_STARTED.inc()
    update_run(run_id, status=RunStatus.running.value)
    try:
        claim = dict(run.claim)
        prompt_version = job.get("prompt_version") or claim.pop("prompt_version", "v1")
        result, trace = run_agent(
            claim,
            correlation_id=job.get("cid", ""),
            run_id=str(run_id),
            prompt_version=prompt_version,
        )
        # Preserve the API/store contract while the graph emits the stable Result.
        rec = Recommendation(
            route=result.route, summary=result.summary, compensation_inr=result.amount,
            citations=result.citations, escalation_reason=result.escalation_reason,
            verified=False,
        )
    except Exception as e:
        span = otel_trace.get_current_span()
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, "agent execution failed"))
        log.exception(json.dumps(f"run {run_id} failed"))
        rec = Recommendation(route=Route.escalate, summary="Run failed; routed to a human officer.",
                             escalation_reason=EscalationReason.provider_degraded)
        trace = [{"step": "error", "detail": str(e)}]
    status = RunStatus.escalated if rec.route == Route.escalate else RunStatus.completed
    update_run(run_id, status=status.value, result=rec.model_dump(mode="json"), trace=trace)
    reason = rec.escalation_reason.value if rec.escalation_reason else "none"
    RUNS.labels(rec.route.value, reason).inc()
    final = next((step for step in reversed(trace) if step.get("step") == "finalize"), {})
    STEPS.observe(final.get("step_count", 0))
    TOOLS.observe(final.get("tool_calls", 0))
    log.info(json.dumps({"run_id": str(run_id), "route": rec.route.value, "cid": job.get("cid")}))


def main() -> None:
    provider = configure_tracing("triage-worker")
    init_db()
    start_http_server(settings.worker_metrics_port)
    # Keep the client read timeout above BRPOP's server-side timeout so an
    # idle queue returns control normally instead of terminating the worker.
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=10)
    log.info(json.dumps("worker started"))
    try:
        while True:
            item = r.brpop(settings.queue_name, timeout=5)
            if item:
                handle(json.loads(item[1]))
    finally:
        provider.shutdown()


if __name__ == "__main__":
    main()

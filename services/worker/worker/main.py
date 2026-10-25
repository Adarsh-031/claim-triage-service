"""Queue consumer with recoverable Redis processing and stale-run reaping."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event, Thread

import redis
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import start_http_server
from triage_common.contracts import EscalationReason, Recommendation, Route, RunStatus
from triage_common.settings import settings
from triage_common.store import get_run, init_db, recover_stale_run, update_run
from triage_common.telemetry import configure_tracing

from .agent.graph import run_agent
from .metrics import ACTIVE, RUN_LATENCY, RUNS, RUNS_STARTED, STEPS, TOOLS

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("worker")

PROCESSING_SUFFIX = ":processing"
BLOCKING_TIMEOUT_S = 5
REDIS_SOCKET_TIMEOUT_S = 10  # Must remain greater than BLMOVE's blocking timeout.
REAPER_INTERVAL_S = 15
RUN_WALL_CLOCK_BUDGET_S = 60
MAX_REQUEUES = 2
REAPER_LOCK_TTL_S = 30


def handle(job: dict) -> bool:
    """Process a claimed queue message; True means its outcome is durably stored."""
    with otel_trace.get_tracer(__name__).start_as_current_span(
        "queue.process",
        context=extract(job.get("trace_context") or {}),
        kind=SpanKind.CONSUMER,
        attributes={
            "run_id": job["run_id"],
            "correlation_id": job.get("cid", ""),
            "messaging.system": "redis",
            "messaging.destination.name": settings.queue_name,
        },
    ), ACTIVE.track_inprogress(), RUN_LATENCY.time():
        return _handle(job)


def _handle(job: dict) -> bool:
    run_id = uuid.UUID(job["run_id"])
    run = get_run(run_id)
    if run is None:
        # No corresponding database row remains; this queue item is an orphan.
        return True
    terminal = {RunStatus.done.value, RunStatus.escalated.value, RunStatus.failed.value, "completed"}
    current_status = getattr(run, "status", RunStatus.queued.value)
    if current_status in terminal or current_status == RunStatus.running.value:
        # A duplicate must not start a second execution or overwrite stored output.
        return True
    if current_status != RunStatus.queued.value:
        return True

    started_at = datetime.now(UTC)
    update_run(run_id, status=RunStatus.running.value, started_at=started_at, finished_at=None, error=None)
    RUNS_STARTED.inc()
    try:
        result, trace = run_agent(run.claim, correlation_id=job.get("cid", ""), run_id=str(run_id))
        rec = Recommendation(
            route=result.route,
            summary=result.summary,
            compensation_inr=result.amount,
            citations=result.citations,
            escalation_reason=result.escalation_reason,
            verified=False,
        )
    except Exception as e:
        span = otel_trace.get_current_span()
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, "agent execution failed"))
        log.exception(json.dumps(f"run {run_id} failed"))
        trace = [{"step": "error", "detail": str(e)}]
        # An execution failure is a failed run, not a business escalation.
        update_run(
            run_id,
            status=RunStatus.failed.value,
            result=None,
            trace=trace,
            error=str(e),
            finished_at=datetime.now(UTC),
        )
        RUNS.labels(Route.escalate.value, EscalationReason.WORKER_FAILURE.value).inc()
        return True

    status = RunStatus.escalated if rec.route == Route.escalate else RunStatus.done
    # Persist the final result/status before the queue item is acknowledged.
    update_run(
        run_id,
        status=status.value,
        result=rec.model_dump(mode="json"),
        trace=trace,
        finished_at=datetime.now(UTC),
        error=None,
    )
    reason = rec.escalation_reason.value if rec.escalation_reason else "none"
    RUNS.labels(rec.route.value, reason).inc()
    final = next((step for step in reversed(trace) if step.get("step") == "finalize"), {})
    STEPS.observe(final.get("step_count", 0))
    TOOLS.observe(final.get("tool_calls", 0))
    log.info(json.dumps({"run_id": str(run_id), "route": rec.route.value, "cid": job.get("cid")}))
    return True


def reap_once(r: redis.Redis) -> dict[str, int]:
    """Reap entries older than the wall-clock budget, guarded by a Redis lease."""
    lock_key = f"{settings.queue_name}:reaper-lock"
    processing_key = f"{settings.queue_name}{PROCESSING_SUFFIX}"
    token = str(uuid.uuid4())
    if not r.set(lock_key, token, nx=True, ex=REAPER_LOCK_TTL_S):
        return {"requeued": 0, "failed": 0}

    requeued = failed = 0
    try:
        for raw in r.lrange(processing_key, 0, -1):
            try:
                job = json.loads(raw)
                run_id = uuid.UUID(job["run_id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                # Malformed processing entries cannot be tied to a database run.
                r.lrem(processing_key, 1, raw)
                continue

            outcome = recover_stale_run(
                run_id,
                stale_after_s=RUN_WALL_CLOCK_BUDGET_S,
                max_requeues=MAX_REQUEUES,
            )
            if outcome in {"terminal", "missing"}:
                r.lrem(processing_key, 1, raw)
            elif outcome == "failed":
                r.lrem(processing_key, 1, raw)
                failed += 1
            elif outcome == "requeued":
                # Database state/retry count is committed before republishing. Keep the
                # processing copy until the queue copy has been published successfully.
                r.lpush(settings.queue_name, raw)
                r.lrem(processing_key, 1, raw)
                requeued += 1
            # not_stale stays in the processing list for its current worker or a later pass.
        return {"requeued": requeued, "failed": failed}
    finally:
        # Atomic compare-and-delete avoids releasing a lease acquired by another worker.
        with suppress(redis.RedisError):
            r.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                token,
            )


def _reaper_loop(r: redis.Redis, stop: Event) -> None:
    """Run recovery independently so an active job cannot block the 15-second cadence."""
    while not stop.is_set():
        try:
            reap_once(r)
        except Exception:
            log.exception("worker reaper iteration failed")
        stop.wait(REAPER_INTERVAL_S)


def main() -> None:
    provider = configure_tracing("triage-worker")
    init_db()
    start_http_server(settings.worker_metrics_port)
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=REDIS_SOCKET_TIMEOUT_S)
    log.info(json.dumps("worker started"))
    processing_key = f"{settings.queue_name}{PROCESSING_SUFFIX}"
    stop_reaper = Event()
    reaper_thread = Thread(
        target=_reaper_loop,
        args=(r, stop_reaper),
        name="run-reaper",
        daemon=True,
    )
    reaper_thread.start()
    try:
        while True:
            item = r.blmove(
                settings.queue_name,
                processing_key,
                timeout=BLOCKING_TIMEOUT_S,
                src="RIGHT",
                dest="LEFT",
            )
            if item is None:
                continue
            acknowledged = False
            try:
                acknowledged = handle(json.loads(item))
            except Exception:
                # Leave the processing item in place: the reaper can recover it if
                # updating/reading Postgres failed before a durable terminal outcome.
                log.exception("queue item processing failed")
            if acknowledged:
                # Acknowledge only after the terminal result or failure is stored.
                r.lrem(processing_key, 1, item)
    finally:
        stop_reaper.set()
        reaper_thread.join(timeout=BLOCKING_TIMEOUT_S + 1)
        provider.shutdown()


if __name__ == "__main__":
    main()

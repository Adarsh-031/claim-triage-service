"""Queue consumer with recoverable Redis processing and stale-run reaping."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event, Thread
from time import sleep

import redis
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import start_http_server
from triage_common.contracts import EscalationReason, Recommendation, Route, RunStatus
from triage_common.settings import settings
from triage_common.store import claim_run, finish_run, get_run, heartbeat_run, init_db, recover_stale_run
from triage_common.telemetry import configure_tracing

from .agent.graph import run_agent
from .metrics import ACTIVE, RUN_LATENCY, RUNS, RUNS_STARTED, STEPS, TOOLS

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("worker")

PROCESSING_SUFFIX = ":processing"
BLOCKING_TIMEOUT_S = 5
REDIS_SOCKET_TIMEOUT_S = 10  # Must remain greater than BLMOVE's blocking timeout.
REAPER_INTERVAL_S = 15
HEARTBEAT_INTERVAL_S = 10
HEARTBEAT_STALE_AFTER_S = HEARTBEAT_INTERVAL_S * 3
MAX_REQUEUES = 2
REAPER_LOCK_TTL_S = 30
REDIS_RETRY_DELAY_S = 1
PUBLIC_WORKER_ERROR = "Automated processing failed. Please retry or route this claim to a human officer."


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


def _heartbeat_loop(run_id: uuid.UUID, attempt: int, stop: Event) -> None:
    """Refresh the current attempt's liveness while a potentially slow graph node runs."""
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        try:
            if not heartbeat_run(run_id, attempt):
                return
        except Exception:
            log.exception("run heartbeat update failed")


def _handle(job: dict) -> bool:
    run_id = uuid.UUID(job["run_id"])
    run = get_run(run_id)
    if run is None:
        return True

    # Compare-and-set claim: only one worker can transition queued -> running.
    attempt = claim_run(run_id, now=datetime.now(UTC))
    if attempt is None:
        return False

    RUNS_STARTED.inc()
    heartbeat_stop = Event()
    heartbeat_thread = Thread(
        target=_heartbeat_loop,
        args=(run_id, attempt, heartbeat_stop),
        name="run-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
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
        except Exception as exc:
            span = otel_trace.get_current_span()
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, "agent execution failed"))
            log.exception(json.dumps(f"run {run_id} failed"))
            trace = [{"step": "error", "detail": str(exc)}]
            # Keep sensitive provider/URL details in the internal trace, not GET /runs.
            rec = Recommendation(
                route=Route.escalate,
                summary="Automated processing failed. Human review is required.",
                escalation_reason=EscalationReason.WORKER_FAILURE,
                verified=False,
            )
            persisted = finish_run(
                run_id,
                attempt,
                status=RunStatus.failed.value,
                result=rec.model_dump(mode="json"),
                trace=trace,
                error=PUBLIC_WORKER_ERROR,
                finished_at=datetime.now(UTC),
            )
            if persisted:
                RUNS.labels("failed", EscalationReason.WORKER_FAILURE.value).inc()
                return True
            log.warning("run %s attempt %s lost ownership before failure result", run_id, attempt)
            return False

        status = RunStatus.escalated if rec.route == Route.escalate else RunStatus.done
        persisted = finish_run(
            run_id,
            attempt,
            status=status.value,
            result=rec.model_dump(mode="json"),
            trace=trace,
            finished_at=datetime.now(UTC),
            error=None,
        )
        if not persisted:
            log.warning("run %s attempt %s lost ownership before finalization", run_id, attempt)
            return False

        reason = rec.escalation_reason.value if rec.escalation_reason else "none"
        RUNS.labels(rec.route.value, reason).inc()
        final = next((step for step in reversed(trace) if step.get("step") == "finalize"), {})
        STEPS.observe(final.get("step_count", 0))
        TOOLS.observe(final.get("tool_calls", 0))
        log.info(json.dumps({"run_id": str(run_id), "route": rec.route.value, "cid": job.get("cid")}))
        return True
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=HEARTBEAT_INTERVAL_S + 1)


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
                stale_after_s=HEARTBEAT_STALE_AFTER_S,
                max_requeues=MAX_REQUEUES,
            )
            if outcome in {"terminal", "missing"}:
                r.lrem(processing_key, 1, raw)
            elif outcome == "failed":
                r.lrem(processing_key, 1, raw)
                RUNS.labels("failed", EscalationReason.WORKER_FAILURE.value).inc()
                failed += 1
            elif outcome == "requeued":
                # RPUSH places this retry at the right end, where BLMOVE consumes next.
                # Retain the processing copy until republishing succeeds.
                r.rpush(settings.queue_name, raw)
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


def consume_once(r: redis.Redis) -> bool:
    """Consume one message without letting a Redis connection error kill the worker."""
    processing_key = f"{settings.queue_name}{PROCESSING_SUFFIX}"
    try:
        item = r.blmove(
            settings.queue_name,
            processing_key,
            timeout=BLOCKING_TIMEOUT_S,
            src="RIGHT",
            dest="LEFT",
        )
    except redis.RedisError:
        log.exception("Redis queue receive failed; retrying")
        sleep(REDIS_RETRY_DELAY_S)
        return False
    if item is None:
        return False

    try:
        acknowledged = handle(json.loads(item))
    except Exception:
        log.exception("queue item processing failed")
        return False

    if acknowledged:
        try:
            r.lrem(processing_key, 1, item)
        except redis.RedisError:
            log.exception("Redis acknowledgement failed; leaving item for reaper recovery")
            sleep(REDIS_RETRY_DELAY_S)
            return False
    return acknowledged


def main() -> None:
    provider = configure_tracing("triage-worker")
    init_db()
    start_http_server(settings.worker_metrics_port)
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=REDIS_SOCKET_TIMEOUT_S)
    log.info(json.dumps("worker started"))
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
            consume_once(r)
    finally:
        stop_reaper.set()
        reaper_thread.join(timeout=BLOCKING_TIMEOUT_S + 1)
        provider.shutdown()


if __name__ == "__main__":
    main()

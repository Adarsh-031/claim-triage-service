"""verifier-worker (#28): consumes drafts from the verify queue and runs the grounding verifier.

Same image as the agent-worker, different entrypoint (`python -m worker.verifier_main`).
The hand-off is a `drafts` row in Postgres plus a message on the verify queue, consumed
with the same reliable-queue pattern as the run queue: BLMOVE into a processing list,
heartbeat while working, acknowledge only after the outcome is stored, and a reaper that
re-queues or escalates entries whose verifier stopped heartbeating.
"""
from __future__ import annotations

import json
import logging
import socket
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event, Thread
from time import sleep

import redis
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind
from prometheus_client import Counter, start_http_server
from triage_common.cache import cache_set, cacheable_result, run_cache_key
from triage_common.contracts import Recommendation, Result, Route, RunStatus
from triage_common.settings import settings
from triage_common.store import (
    claim_draft,
    expire_waiting_drafts,
    finish_verified_run,
    get_draft,
    get_run,
    heartbeat_draft,
    init_db,
    record_verifier_outcomes,
    recover_stale_draft,
    request_retry,
)
from triage_common.telemetry import configure_tracing

from .agent.graph import dump_state, load_state, verify_once
from .metrics import RUNS
from .tools.corpus import get_corpus

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("verifier-worker")

PROCESSING_SUFFIX = ":processing"
BLOCKING_TIMEOUT_S = 5
REDIS_SOCKET_TIMEOUT_S = 10  # Must remain greater than BLMOVE's blocking timeout.
REAPER_INTERVAL_S = 15
HEARTBEAT_INTERVAL_S = 10
HEARTBEAT_STALE_AFTER_S = HEARTBEAT_INTERVAL_S * 3
MAX_RECLAIMS = 2
REAPER_LOCK_TTL_S = 30
REDIS_RETRY_DELAY_S = 1

VERIFICATIONS = Counter("verifier_outcomes_total", "Verifier decisions", ["decision"])
r_client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=REDIS_SOCKET_TIMEOUT_S)


def _processing_key() -> str:
    return f"{settings.verify_queue_name}{PROCESSING_SUFFIX}"


def _heartbeat_loop(run_id: uuid.UUID, run_attempt: int, claim: int, stop: Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        try:
            if not heartbeat_draft(run_id, run_attempt, claim):
                return
        except Exception:
            log.exception("draft heartbeat update failed")


def handle(job: dict) -> bool:
    """Verify one handed-off draft; True means its outcome is durably stored (or moot)."""
    with otel_trace.get_tracer(__name__).start_as_current_span(
        "verify.process", context=extract(job.get("trace_context") or {}), kind=SpanKind.CONSUMER,
        attributes={"run_id": job["run_id"], "correlation_id": job.get("cid", ""),
                    "messaging.system": "redis", "messaging.destination.name": settings.verify_queue_name,
                    "host.name": socket.gethostname()},
    ):
        return _handle(job)


def _handle(job: dict) -> bool:
    run_id, run_attempt = uuid.UUID(job["run_id"]), int(job["run_attempt"])
    claim = claim_draft(run_id, run_attempt, host=socket.gethostname(), now=datetime.now(UTC))
    if claim is None:
        # Already verified, superseded by a newer attempt, or escalated by the reaper.
        return True
    stop = Event()
    beat = Thread(target=_heartbeat_loop, args=(run_id, run_attempt, claim, stop),
                  name="draft-heartbeat", daemon=True)
    beat.start()
    try:
        draft = get_draft(run_id, run_attempt)
        state, decision = verify_once(load_state(dict(draft.state)))
        if decision == "retry":
            return _send_back(job, run_id, run_attempt, claim, state)
        return _publish(job, run_id, run_attempt, claim, state)
    finally:
        stop.set()
        beat.join(timeout=HEARTBEAT_INTERVAL_S + 1)


def _send_back(job: dict, run_id: uuid.UUID, run_attempt: int, claim: int, state: dict) -> bool:
    """Retry: the agent-worker expands retrieval and re-drafts, then hands off again."""
    if not request_retry(run_id, run_attempt, claim, dump_state(state), trace=state.get("trace", [])):
        log.warning("run %s lost verifier ownership before retry", run_id)
        return False
    carrier: dict[str, str] = {}
    inject(carrier)
    message = {"run_id": str(run_id), "resume": True, "cid": job.get("cid", ""),
               "client_id": job.get("client_id", "unknown"), "prompt_version": job.get("prompt_version"),
               "trace_context": carrier}
    for attempt in range(3):
        try:
            r_client.lpush(settings.queue_name, json.dumps(message))
            break
        except redis.RedisError:
            # Same window as the API's commit-then-publish; the run row is safely queued.
            log.exception("run queue publish failed for verifier retry of run %s (try %s)", run_id, attempt + 1)
            sleep(REDIS_RETRY_DELAY_S)
    VERIFICATIONS.labels("retry").inc()
    with suppress(Exception):
        record_verifier_outcomes(run_id, run_attempt, [t for t in state["trace"][-1:] if t.get("step") == "verify"])
    return True


def _publish(job: dict, run_id: uuid.UUID, run_attempt: int, claim: int, state: dict) -> bool:
    result = Result.model_validate(state["result"])
    trace = state.get("trace", [])
    final = next((t for t in reversed(trace) if t.get("step") == "finalize"), {})
    rec = Recommendation(route=result.route, summary=result.summary, compensation_inr=result.amount,
                         citations=result.citations, escalation_reason=result.escalation_reason,
                         verified=final.get("verified") is True)
    status = RunStatus.escalated if rec.route == Route.escalate else RunStatus.done
    payload = rec.model_dump(mode="json")
    if not finish_verified_run(run_id, run_attempt, claim, status=status.value, result=payload,
                               trace=trace, finished_at=datetime.now(UTC), error=None):
        log.warning("run %s lost verifier ownership before finalization", run_id)
        return False
    VERIFICATIONS.labels("verified" if rec.verified else "escalated").inc()
    RUNS.labels(rec.route.value, rec.escalation_reason.value if rec.escalation_reason else "none").inc()
    try:
        # Only this attempt's verify step; earlier attempts were stored when they sent the run back.
        record_verifier_outcomes(run_id, run_attempt, [t for t in trace if t.get("step") == "verify"
                                                       and t.get("attempt") == state.get("attempts", 0)])
    except Exception:
        log.exception("could not store verifier outcomes for run %s", run_id)
    _cache(job, run_id, status, payload)
    log.info(json.dumps({"run_id": str(run_id), "route": rec.route.value, "verified": rec.verified,
                         "cid": job.get("cid")}))
    return True


def _cache(job: dict, run_id: uuid.UUID, status: RunStatus, payload: dict) -> None:
    if not settings.cache_enabled or not cacheable_result(payload):
        return
    try:
        run = get_run(run_id)
        claim = dict(run.claim)
        legacy_version = claim.pop("prompt_version", settings.prompt_version)
        key = run_cache_key(claim, job.get("prompt_version") or legacy_version, get_corpus().version,
                            job.get("client_id", "unknown"))
        cache_set(r_client, key, {"status": status.value, "result": payload}, settings.cache_ttl_s)
    except Exception:
        log.exception("could not cache verified result for run %s", run_id)


def reap_once(r: redis.Redis) -> dict[str, int]:
    """Recover verify entries whose verifier died, and escalate drafts nobody picked up."""
    lock_key = f"{settings.verify_queue_name}:reaper-lock"
    token = str(uuid.uuid4())
    if not r.set(lock_key, token, nx=True, ex=REAPER_LOCK_TTL_S):
        return {"requeued": 0, "failed": 0}
    requeued = failed = 0
    try:
        for raw in r.lrange(_processing_key(), 0, -1):
            try:
                job = json.loads(raw)
                run_id, run_attempt = uuid.UUID(job["run_id"]), int(job["run_attempt"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                r.lrem(_processing_key(), 1, raw)
                continue
            outcome = recover_stale_draft(run_id, run_attempt, stale_after_s=HEARTBEAT_STALE_AFTER_S,
                                          max_reclaims=MAX_RECLAIMS)
            if outcome in {"done", "missing"}:
                r.lrem(_processing_key(), 1, raw)
            elif outcome == "failed":
                r.lrem(_processing_key(), 1, raw)
                VERIFICATIONS.labels("worker_failure").inc()
                failed += 1
            elif outcome == "requeued":
                r.rpush(settings.verify_queue_name, raw)
                r.lrem(_processing_key(), 1, raw)
                requeued += 1
        failed += len(expire_waiting_drafts(wait_s=settings.verify_wait_s))
        return {"requeued": requeued, "failed": failed}
    finally:
        with suppress(redis.RedisError):
            r.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                   1, lock_key, token)


def _reaper_loop(r: redis.Redis, stop: Event) -> None:
    while not stop.is_set():
        try:
            reap_once(r)
        except Exception:
            log.exception("verifier reaper iteration failed")
        stop.wait(REAPER_INTERVAL_S)


def consume_once(r: redis.Redis) -> bool:
    try:
        item = r.blmove(settings.verify_queue_name, _processing_key(), timeout=BLOCKING_TIMEOUT_S,
                        src="RIGHT", dest="LEFT")
    except redis.RedisError:
        log.exception("Redis verify queue receive failed; retrying")
        sleep(REDIS_RETRY_DELAY_S)
        return False
    if item is None:
        return False
    try:
        acknowledged = handle(json.loads(item))
    except Exception:
        log.exception("verify item processing failed; leaving it for the reaper")
        return False
    if acknowledged:
        try:
            r.lrem(_processing_key(), 1, item)
        except redis.RedisError:
            log.exception("Redis acknowledgement failed; leaving item for reaper recovery")
            sleep(REDIS_RETRY_DELAY_S)
            return False
    return acknowledged


def main() -> None:
    provider = configure_tracing("triage-verifier")
    init_db()
    start_http_server(settings.verifier_metrics_port)
    log.info(json.dumps(f"verifier-worker started on {socket.gethostname()}"))
    stop = Event()
    reaper = Thread(target=_reaper_loop, args=(r_client, stop), name="verify-reaper", daemon=True)
    reaper.start()
    try:
        while True:
            consume_once(r_client)
    finally:
        stop.set()
        reaper.join(timeout=BLOCKING_TIMEOUT_S + 1)
        provider.shutdown()


if __name__ == "__main__":
    main()

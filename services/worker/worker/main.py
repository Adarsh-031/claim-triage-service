"""Queue consumer. One process = one agent worker; scale with `--scale worker=N` (A7)."""
from __future__ import annotations

import json
import logging
import uuid

import redis
from triage_common.contracts import EscalationReason, Recommendation, Route, RunStatus
from triage_common.settings import settings
from triage_common.store import get_run, init_db, update_run

from .agent.graph import run_agent

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("worker")


def handle(job: dict) -> None:
    run_id = uuid.UUID(job["run_id"])
    run = get_run(run_id)
    if run is None:
        return
    update_run(run_id, status=RunStatus.running.value)
    try:
        rec, trace = run_agent(run.claim, correlation_id=job.get("cid", ""))
    except Exception as e:
        log.exception(json.dumps(f"run {run_id} failed"))
        rec = Recommendation(route=Route.escalate, summary="Run failed; routed to a human officer.",
                             escalation_reason=EscalationReason.provider_degraded)
        trace = [{"step": "error", "detail": str(e)}]
    status = RunStatus.escalated if rec.route == Route.escalate else RunStatus.completed
    update_run(run_id, status=status.value, result=rec.model_dump(mode="json"), trace=trace)
    log.info(json.dumps({"run_id": str(run_id), "route": rec.route.value, "cid": job.get("cid")}))


def main() -> None:
    init_db()
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    log.info(json.dumps("worker started"))
    while True:
        item = r.brpop(settings.queue_name, timeout=5)
        if item:
            handle(json.loads(item[1]))


if __name__ == "__main__":
    main()

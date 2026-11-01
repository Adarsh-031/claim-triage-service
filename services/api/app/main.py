"""Claim intake API. Validates at the boundary (D1), quarantines bad input, enqueues runs (A4)."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime

import redis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from triage_common import guardrails
from triage_common.cache import allow_run, cache_get, current_corpus_version, run_cache_key
from triage_common.contracts import (
    TERMINAL_STATUSES,
    ClaimIn,
    EscalationReason,
    FeedbackIn,
    Recommendation,
    Route,
    RunOut,
    RunStatus,
    SubmitOut,
)
from triage_common.logs import configure_logging
from triage_common.settings import settings
from triage_common.store import (
    Feedback,
    Quarantine,
    Run,
    SessionLocal,
    engine,
    get_run,
    init_db,
    list_quarantine,
)
from triage_common.telemetry import configure_tracing

app = FastAPI(title="Claim Triage Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
r = redis.Redis.from_url(settings.redis_url, decode_responses=True)

CLAIMS = Counter("claims_submitted_total", "Claims accepted for processing")
QUARANTINED = Counter("claims_quarantined_total", "Claims rejected at the boundary")
GUARDRAIL_BLOCKS = Counter("guardrail_blocks_total", "Guardrail hits that blocked a claim",
                           ["stage", "kind", "rule"])
CACHE_HITS = Counter("api_cache_hits_total", "API cache hits by component", ["component"])
RAW_BODY_LIMIT = 16 * 1024
SUBMIT_LATENCY = Histogram("submit_latency_seconds", "Latency of POST /v1/claims")
RATE_LIMIT_REDIS_ERRORS = Counter(
    "rate_limit_redis_errors_total", "Rate limit checks unavailable because Redis failed"
)
configure_logging("triage-api")
log = logging.getLogger("triage-api")


@app.on_event("startup")
def _startup() -> None:
    app.state.tracing = configure_tracing("triage-api")
    init_db()


@app.on_event("shutdown")
def _shutdown() -> None:
    if provider := getattr(app.state, "tracing", None):
        provider.shutdown()


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
    request.state.cid = cid
    resp = await call_next(request)
    resp.headers["x-correlation-id"] = cid
    return resp


def _client_id(request: Request) -> str:
    # Client-supplied IDs are unauthenticated and cannot safely define quotas
    # or cache namespaces. Honor only the source address seen by this service.
    return (request.client.host if request.client else "unknown")[:255]


def _rate_limit(client: str) -> tuple[bool, int] | None:
    try:
        return allow_run(r, client, settings.rate_limit_per_min)
    except redis.RedisError:
        RATE_LIMIT_REDIS_ERRORS.inc()
        log.warning("Redis unavailable during rate-limit check; rejecting request with 503", exc_info=True)
        return None


@app.exception_handler(RequestValidationError)
async def quarantine_invalid(request: Request, exc: RequestValidationError):
    """Invalid claims are stored in quarantine with field-level errors; no agent run starts."""
    raw = await request.body()
    body = raw.decode("utf-8", errors="replace").encode("utf-8")[:RAW_BODY_LIMIT].decode("utf-8", errors="ignore")
    errors = [
        {
            "field": ".".join(map(str, error["loc"][1:])) or "body",
            "msg": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    qid = None
    if request.url.path == "/v1/claims":
        rate_limit = _rate_limit(_client_id(request))
        if rate_limit is None:
            return JSONResponse(status_code=503, content={"detail": "rate limiter unavailable"})
        allowed, retry_after = rate_limit
        if not allowed:
            return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"},
                                headers={"Retry-After": str(retry_after)})
        with SessionLocal() as s, s.begin():
            q = Quarantine(raw_body=body, errors=errors, client_id=_client_id(request))
            s.add(q)
            s.flush()
            qid = str(q.id)
        QUARANTINED.inc()
    return JSONResponse(status_code=422, content={"quarantine_id": qid, "errors": errors})


@app.post("/v1/claims", response_model=SubmitOut, status_code=202)
def submit_claim(claim: ClaimIn, request: Request):
    prompt_version = request.headers.get("x-prompt-version", settings.prompt_version)
    if not re.fullmatch(r"v[12]", prompt_version):
        raise HTTPException(400, "x-prompt-version must be 'v1' or 'v2'")
    with SUBMIT_LATENCY.time():
        rate_limit = _rate_limit(_client_id(request))
        if rate_limit is None:
            raise HTTPException(503, "rate limiter unavailable")
        allowed, retry_after = rate_limit
        if not allowed:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry_after)})
        claim_data = claim.model_dump(mode="json")
        client_id = _client_id(request)
        hits = guardrails.screen_input(claim_data)
        if guardrails.blocked(hits):
            # Blocked input never reaches the queue or a model; the run is escalated at once.
            rec = Recommendation(route=Route.ESCALATE, escalation_reason=EscalationReason.GUARDRAIL_BLOCKED,
                                 summary=guardrails.block_summary(hits))
            step = {"step": "guardrail", "stage": "input", "service": "api", "blocked": True,
                    "prompt_version": prompt_version, "hits": [h.as_dict() for h in hits]}
            with SessionLocal() as s, s.begin():
                now = datetime.now(UTC)
                run = Run(claim=claim_data, status=RunStatus.escalated.value, result=rec.model_dump(mode="json"),
                          trace=[step], started_at=now, finished_at=now)
                s.add(run)
                s.flush()
                run_id = run.id
            for h in hits:
                GUARDRAIL_BLOCKS.labels(h.stage, h.kind, h.rule).inc()
            log.info("guardrail blocked claim run_id=%s rules=%s", run_id, [h.rule for h in hits])
            CLAIMS.inc()
            return SubmitOut(run_id=run_id, status=RunStatus.escalated, poll_url=f"/v1/runs/{run_id}")
        corpus_version = current_corpus_version(r) if settings.cache_enabled else None
        if corpus_version:
            key = run_cache_key(claim_data, prompt_version, corpus_version, client_id)
            cached = cache_get(r, key)
            if cached:
                with SessionLocal() as s, s.begin():
                    run = Run(
                        claim=claim_data,
                        status=cached["status"],
                        result=cached["result"],
                        trace=[{"step": "cache", "hit": True, "corpus_version": corpus_version}],
                    )
                    s.add(run)
                    s.flush()
                    run_id = run.id
                CACHE_HITS.labels("run").inc()
                CLAIMS.inc()
                return SubmitOut(run_id=run_id, status=RunStatus(cached["status"]), poll_url=f"/v1/runs/{run_id}")
        with SessionLocal() as s, s.begin():
            run = Run(claim=claim_data, status=RunStatus.queued.value)
            s.add(run)
            s.flush()
            run_id = run.id
        with trace.get_tracer(__name__).start_as_current_span("queue.publish", kind=SpanKind.PRODUCER) as span:
            span.set_attributes({"run_id": str(run_id), "correlation_id": request.state.cid,
                                 "messaging.system": "redis", "messaging.destination.name": settings.queue_name})
            carrier = {}
            inject(carrier)
            r.lpush(settings.queue_name, json.dumps({"run_id": str(run_id), "cid": request.state.cid,
                                                     "prompt_version": prompt_version,
                                                     "client_id": client_id,
                                                     "trace_context": carrier}))
        CLAIMS.inc()
        return SubmitOut(run_id=run_id, status=RunStatus.queued, poll_url=f"/v1/runs/{run_id}")


@app.get("/v1/quarantine")
def get_quarantine(limit: int = Query(default=50, ge=1, le=100)):
    """Return the newest quarantined submissions for the demo console."""
    return [
        {
            "id": str(item.id),
            "received_at": item.received_at.isoformat(),
            "raw_body": item.raw_body,
            "errors": item.errors,
            "client_id": item.client_id,
        }
        for item in list_quarantine(limit)
    ]


@app.get("/v1/runs/{run_id}", response_model=RunOut)
def get_run_status(run_id: uuid.UUID):
    run = get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return RunOut(
        run_id=run.id,
        status=run.status,
        terminal=str(run.status) in TERMINAL_STATUSES,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        result=Recommendation(**run.result) if run.result else None,
        error=run.error,
    )


@app.get("/v1/runs/{run_id}/trace")
def get_run_trace(run_id: uuid.UUID):
    run = get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    steps = run.trace or []
    final = next((s for s in reversed(steps) if s.get("step") == "finalize"), {})
    # Tokens and cost were recorded per call at the time of the call (finalize sums them).
    return {"run_id": str(run.id), "status": run.status, "steps": steps,
            "usage": final.get("usage"), "llm_calls": final.get("llm_calls", [])}


@app.post("/v1/runs/{run_id}/feedback", status_code=201)
def feedback(run_id: uuid.UUID, fb: FeedbackIn):
    if not get_run(run_id):
        raise HTTPException(404, "run not found")
    with SessionLocal() as s, s.begin():
        s.add(Feedback(run_id=run_id, agree=fb.agree, note=fb.note))
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        r.ping()
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not ready", "error": str(e)})
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")

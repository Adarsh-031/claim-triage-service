"""Run store shared by API and worker (Postgres via SQLAlchemy)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .contracts import TERMINAL_STATUSES, EscalationReason, Recommendation, Route
from .settings import settings


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    claim: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Counts successful claims, including the initial attempt; used to fence stale workers.
    requeue_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Claims caused by verifier retries (#28), not crashes; excluded from the recovery limit.
    resume_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Quarantine(Base):
    __tablename__ = "quarantine"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    raw_body: Mapped[str] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSON)
    client_id: Mapped[str] = mapped_column(String(255), default="unknown")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID]
    agree: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Draft(Base):
    """Agent-worker -> verifier-worker hand-off (#28), keyed by run_id + the run claim attempt.

    status: pending (in q:verify) -> verifying (claimed) -> finalized | retry (sent back to
    q:runs) -> resumed (an agent-worker picked the retry up) | failed (escalated by the reaper).
    """
    __tablename__ = "drafts"
    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    verify_attempt: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # Counts verifier claims; fences a stale verifier exactly like runs.requeue_count.
    claim_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    agent_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verifier_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class VerifierOutcome(Base):
    """One grounding-verifier attempt for a run (#27); the full detail also stays in runs.trace."""
    __tablename__ = "verifier_outcomes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    run_attempt: Mapped[int] = mapped_column(Integer)
    verifier_attempt: Mapped[int] = mapped_column(Integer)
    verified: Mapped[bool] = mapped_column(Boolean)
    failed_checks: Mapped[list] = mapped_column(JSON)
    passed_checks: Mapped[list] = mapped_column(JSON)
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    # Schema upgrades are handled by deploy/postgres/init.sql for fresh databases.
    Base.metadata.create_all(engine)


def list_quarantine(limit: int = 50) -> list[Quarantine]:
    with SessionLocal() as s:
        return list(s.scalars(select(Quarantine).order_by(Quarantine.received_at.desc()).limit(limit)))


def get_run(run_id: uuid.UUID) -> Run | None:
    with SessionLocal() as s:
        return s.get(Run, run_id)


def update_run(run_id: uuid.UUID, **fields) -> None:
    with SessionLocal() as s, s.begin():
        run = s.get(Run, run_id)
        if run:
            for k, v in fields.items():
                setattr(run, k, v)


def claim_run(run_id: uuid.UUID, *, now: datetime | None = None) -> int | None:
    """Atomically claim a queued run and return its claim-attempt number."""
    now = now or _now()
    with SessionLocal() as session, session.begin():
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status="running", started_at=now, heartbeat_at=now, finished_at=None,
                    error=None, requeue_count=Run.requeue_count + 1, updated_at=now)
        )
        if result.rowcount != 1:
            return None
        return session.scalar(select(Run.requeue_count).where(Run.id == run_id))


def heartbeat_run(run_id: uuid.UUID, attempt: int, *, now: datetime | None = None) -> bool:
    """Refresh liveness only while this attempt still owns the running row."""
    now = now or _now()
    with SessionLocal() as session, session.begin():
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "running", Run.requeue_count == attempt)
            .values(heartbeat_at=now, updated_at=now)
        )
        return result.rowcount == 1


def finish_run(run_id: uuid.UUID, attempt: int, **fields) -> bool:
    """Persist a result only if this worker still owns the current running attempt."""
    values = {**fields, "heartbeat_at": None, "updated_at": _now()}
    with SessionLocal() as session, session.begin():
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "running", Run.requeue_count == attempt)
            .values(**values)
        )
        return result.rowcount == 1


def recover_stale_run_state(
    run: Run | None, *, now: datetime, stale_after_s: int = 30, max_requeues: int = 2
) -> str:
    """Apply recovery decisions without database or Redis I/O."""
    if run is None:
        return "missing"
    if run.status in TERMINAL_STATUSES:
        return "terminal"
    if run.status not in {"queued", "running"}:
        return "not_stale"

    if run.status == "running":
        anchor = getattr(run, "heartbeat_at", None) or run.started_at
    else:
        anchor = run.updated_at or run.created_at
    if anchor is None:
        return "not_stale"
    if anchor.tzinfo is None and now.tzinfo is not None:
        anchor = anchor.replace(tzinfo=now.tzinfo)
    elif anchor.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=anchor.tzinfo)
    if anchor > now - timedelta(seconds=stale_after_s):
        return "not_stale"

    # The initial claim is counted, so permit it plus the configured recovery claims.
    # Verifier-requested resumes (#28) are planned work, not crash recoveries.
    if run.requeue_count - (getattr(run, "resume_count", 0) or 0) >= max_requeues + 1:
        run.status = "failed"
        run.error = "Automated processing failed after recovery attempts; human review is required."
        run.result = Recommendation(
            route=Route.ESCALATE,
            summary="Automated processing failed after recovery attempts. Human review is required.",
            escalation_reason=EscalationReason.WORKER_FAILURE,
            verified=False,
        ).model_dump(mode="json")
        run.finished_at = now
        run.heartbeat_at = None
        run.updated_at = now
        return "failed"

    # Count a retry only when a worker successfully claims the queued run.
    run.status = "queued"
    run.started_at = None
    run.heartbeat_at = None
    run.finished_at = None
    run.error = None
    run.updated_at = now
    return "requeued"


def recover_stale_run(
    run_id: uuid.UUID,
    *,
    now: datetime | None = None,
    stale_after_s: int = 60,
    max_requeues: int = 2,
) -> str:
    """Atomically advance a stale run's recovery state.

    Returns requeued, failed, not_stale, terminal, or missing. Row locking and
    status checks prevent the reaper from overwriting a result stored concurrently.
    """
    now = now or _now()
    with SessionLocal() as session, session.begin():
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        return recover_stale_run_state(run, now=now, stale_after_s=stale_after_s, max_requeues=max_requeues)


def record_verifier_outcomes(run_id: uuid.UUID, run_attempt: int, outcomes: list[dict]) -> None:
    """Store each verify step from the agent trace; one row per verifier attempt."""
    if not outcomes:
        return
    with SessionLocal() as session, session.begin():
        session.add_all([
            VerifierOutcome(
                run_id=run_id, run_attempt=run_attempt, verifier_attempt=int(o.get("attempt", 0)),
                verified=bool(o.get("verified")), failed_checks=list(o.get("failed", [])),
                passed_checks=list(o.get("passed", [])),
                detail={k: o.get(k) for k in ("prompt_version", "llm_called", "lower_protection", "error")},
            )
            for o in outcomes
        ])


# --- Verifier hand-off (#28) ------------------------------------------------------------

class _OwnershipLost(Exception):
    """Raised inside a transaction to roll back both the draft and run updates."""

def _escalated_failure(summary: str) -> dict:
    return Recommendation(route=Route.ESCALATE, summary=summary,
                          escalation_reason=EscalationReason.VERIFICATION_FAILED,
                          verified=False).model_dump(mode="json")


def handoff_draft(run_id: uuid.UUID, attempt: int, verify_attempt: int, state: dict, *,
                  host: str, trace: list) -> bool:
    """Store the draft and move the run running -> verifying, atomically and fenced by attempt."""
    now = _now()
    with SessionLocal() as session, session.begin():
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "running", Run.requeue_count == attempt)
            .values(status="verifying", heartbeat_at=None, trace=trace, updated_at=now)
        )
        if result.rowcount != 1:
            return False
        session.merge(Draft(run_id=run_id, run_attempt=attempt, verify_attempt=verify_attempt, state=state,
                            status="pending", claim_count=0, agent_host=host, created_at=now, updated_at=now))
        return True


def get_draft(run_id: uuid.UUID, run_attempt: int) -> Draft | None:
    with SessionLocal() as session:
        return session.get(Draft, (run_id, run_attempt))


def claim_draft(run_id: uuid.UUID, run_attempt: int, *, host: str, now: datetime | None = None) -> int | None:
    """pending -> verifying for the run's current attempt; returns this verifier's claim number."""
    now = now or _now()
    with SessionLocal() as session, session.begin():
        current = session.scalar(select(Run.requeue_count).where(Run.id == run_id, Run.status == "verifying"))
        if current != run_attempt:
            return None
        result = session.execute(
            sa_update(Draft)
            .where(Draft.run_id == run_id, Draft.run_attempt == run_attempt, Draft.status == "pending")
            .values(status="verifying", claim_count=Draft.claim_count + 1, verifier_host=host,
                    heartbeat_at=now, updated_at=now)
        )
        if result.rowcount != 1:
            return None
        return session.scalar(select(Draft.claim_count).where(Draft.run_id == run_id,
                                                              Draft.run_attempt == run_attempt))


def _owned_draft(run_id: uuid.UUID, run_attempt: int, claim: int):
    return (Draft.run_id == run_id, Draft.run_attempt == run_attempt,
            Draft.status == "verifying", Draft.claim_count == claim)


def heartbeat_draft(run_id: uuid.UUID, run_attempt: int, claim: int, *, now: datetime | None = None) -> bool:
    now = now or _now()
    with SessionLocal() as session, session.begin():
        result = session.execute(sa_update(Draft).where(*_owned_draft(run_id, run_attempt, claim))
                                 .values(heartbeat_at=now, updated_at=now))
        return result.rowcount == 1


def finish_verified_run(run_id: uuid.UUID, run_attempt: int, claim: int, **fields) -> bool:
    """Verifier publishes the final result only while it still owns the draft and the run."""
    now = _now()
    try:
        return _finish_verified(run_id, run_attempt, claim, now, fields)
    except _OwnershipLost:
        return False


def _finish_verified(run_id, run_attempt, claim, now, fields) -> bool:
    with SessionLocal() as session, session.begin():
        owned = session.execute(sa_update(Draft).where(*_owned_draft(run_id, run_attempt, claim))
                                .values(status="finalized", heartbeat_at=None, updated_at=now))
        if owned.rowcount != 1:
            return False
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "verifying", Run.requeue_count == run_attempt)
            .values(**fields, heartbeat_at=None, updated_at=now)
        )
        if result.rowcount != 1:
            raise _OwnershipLost
        return True


def request_retry(run_id: uuid.UUID, run_attempt: int, claim: int, state: dict, *, trace: list) -> bool:
    """Send the run back to the agent-worker: draft -> retry, run verifying -> queued."""
    now = _now()
    try:
        return _request_retry(run_id, run_attempt, claim, state, trace, now)
    except _OwnershipLost:
        return False


def _request_retry(run_id, run_attempt, claim, state, trace, now) -> bool:
    with SessionLocal() as session, session.begin():
        owned = session.execute(sa_update(Draft).where(*_owned_draft(run_id, run_attempt, claim))
                                .values(status="retry", state=state, heartbeat_at=None, updated_at=now))
        if owned.rowcount != 1:
            return False
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "verifying", Run.requeue_count == run_attempt)
            .values(status="queued", trace=trace, resume_count=Run.resume_count + 1,
                    started_at=None, heartbeat_at=None, updated_at=now)
        )
        if result.rowcount != 1:
            raise _OwnershipLost
        return True


def take_retry_draft(run_id: uuid.UUID, new_attempt: int) -> dict | None:
    """The agent-worker that claimed a resumed run takes the newest retry draft exactly once."""
    with SessionLocal() as session, session.begin():
        draft = session.scalar(
            select(Draft).where(Draft.run_id == run_id, Draft.status.in_(("retry", "resumed")),
                   Draft.run_attempt < new_attempt)
            .order_by(Draft.run_attempt.desc()).limit(1).with_for_update()
        )
        if draft is None:
            return None
        draft.status = "resumed"
        draft.updated_at = _now()
        return dict(draft.state)


def _fail_verifying_run(session, run_id: uuid.UUID, run_attempt: int, summary: str, now: datetime) -> bool:
    result = session.execute(
        sa_update(Run)
        .where(Run.id == run_id, Run.status == "verifying", Run.requeue_count == run_attempt)
        .values(status="escalated", result=_escalated_failure(summary), finished_at=now,
                heartbeat_at=None, updated_at=now)
    )
    return result.rowcount == 1


def _aware(value: datetime | None, now: datetime) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value


def recover_stale_draft(run_id: uuid.UUID, run_attempt: int, *, stale_after_s: float = 30,
                        max_reclaims: int = 2, now: datetime | None = None) -> str:
    """For an entry in the verify processing list: requeued, failed, not_stale, done or missing.

    A verifier killed mid-run stops heartbeating; its draft goes back to pending for another
    verifier, up to max_reclaims times, then the run escalates. Nothing is left in limbo.
    """
    now = now or _now()
    with SessionLocal() as session, session.begin():
        draft = session.scalar(select(Draft).where(Draft.run_id == run_id, Draft.run_attempt == run_attempt)
                               .with_for_update())
        if draft is None:
            return "missing"
        if draft.status != "verifying":
            return "done"
        anchor = _aware(draft.heartbeat_at or draft.updated_at, now)
        if anchor is not None and anchor > now - timedelta(seconds=stale_after_s):
            return "not_stale"
        if draft.claim_count >= max_reclaims + 1:
            draft.status = "failed"
            draft.updated_at = now
            _fail_verifying_run(session, run_id, run_attempt,
                                "Grounding verifier failed repeatedly. Human review is required.", now)
            return "failed"
        draft.status = "pending"
        draft.heartbeat_at = None
        draft.updated_at = now
        return "requeued"


def expire_waiting_drafts(*, wait_s: float, now: datetime | None = None) -> list[uuid.UUID]:
    """Escalate drafts no verifier picked up in time (verifier down, or the queue push was lost)."""
    now = now or _now()
    cutoff = now - timedelta(seconds=wait_s)
    expired = []
    with SessionLocal() as session, session.begin():
        drafts = session.scalars(select(Draft).where(Draft.status == "pending", Draft.updated_at < cutoff)
                                 .with_for_update(skip_locked=True)).all()
        for draft in drafts:
            draft.status = "failed"
            draft.updated_at = now
            if _fail_verifying_run(session, draft.run_id, draft.run_attempt,
                                   "Grounding verifier unavailable. Human review is required.", now):
                expired.append(draft.run_id)
    return expired

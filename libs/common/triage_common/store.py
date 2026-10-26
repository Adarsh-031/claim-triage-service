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
    requeue_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
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


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    # Schema changes are applied through deploy/postgres/init.sql for fresh local volumes.
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
    """Atomically claim a queued run and return its recovery-attempt number."""
    now = now or _now()
    with SessionLocal() as session, session.begin():
        result = session.execute(
            sa_update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status="running", started_at=now, heartbeat_at=now, finished_at=None,
                    error=None, updated_at=now)
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

    if run.requeue_count >= max_requeues:
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

    run.requeue_count += 1
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

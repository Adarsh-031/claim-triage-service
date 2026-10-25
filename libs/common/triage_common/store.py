"""Run store shared by API and worker (Postgres via SQLAlchemy)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

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
    requeue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    Base.metadata.create_all(engine)
    # create_all does not add columns to an existing runs table.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS requeue_count integer NOT NULL DEFAULT 0"
        )
        connection.exec_driver_sql("ALTER TABLE runs ADD COLUMN IF NOT EXISTS started_at timestamptz")
        connection.exec_driver_sql("ALTER TABLE runs ADD COLUMN IF NOT EXISTS finished_at timestamptz")
        # Older versions called the successful terminal state completed.
        connection.exec_driver_sql("UPDATE runs SET status = 'done' WHERE status = 'completed'")


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
    cutoff = now - timedelta(seconds=stale_after_s)
    terminal_statuses = {"done", "completed", "escalated", "failed"}
    with SessionLocal() as session, session.begin():
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None:
            return "missing"
        if run.status in terminal_statuses:
            return "terminal"
        anchor = run.started_at or run.updated_at or run.created_at
        if anchor is not None and anchor > cutoff:
            return "not_stale"
        if run.requeue_count >= max_requeues:
            run.status = "failed"
            run.error = f"Run exceeded {max_requeues} recovery attempts after {stale_after_s}s"
            run.finished_at = now
            run.updated_at = now
            return "failed"
        run.requeue_count += 1
        run.status = "queued"
        run.started_at = None
        run.finished_at = None
        run.error = None
        run.updated_at = now
        return "requeued"

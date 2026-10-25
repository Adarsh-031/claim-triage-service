"""Run store shared by API and worker (Postgres via SQLAlchemy)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, create_engine, inspect, select
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
    # create_all does not evolve an existing table. Upgrade the original demo schema
    # in place so deployments with payload/created_at columns remain readable.
    columns = {column["name"] for column in inspect(engine).get_columns("quarantine")}
    dialect = engine.dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    with engine.begin() as conn:
        if "raw_body" not in columns:
            conn.exec_driver_sql("ALTER TABLE quarantine ADD COLUMN raw_body TEXT")
            if "payload" in columns:
                conn.exec_driver_sql("UPDATE quarantine SET raw_body = payload WHERE raw_body IS NULL")
        if "received_at" not in columns:
            conn.exec_driver_sql(f"ALTER TABLE quarantine ADD COLUMN received_at {timestamp_type}")
            if "created_at" in columns:
                conn.exec_driver_sql("UPDATE quarantine SET received_at = created_at WHERE received_at IS NULL")
        if "client_id" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE quarantine ADD COLUMN client_id varchar(255) NOT NULL DEFAULT 'unknown'"
            )


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

"""Structured JSON logs that carry ``run_id`` and the OTel ``trace_id`` (issue #33).

``configure_logging(service)`` once per process; wrap each run in ``log_context(run_id)``.
Every line is then one JSON object, so a run can be followed across api, agent-worker and
verifier-worker with ``docker compose logs | grep <run_id>`` or by its Jaeger trace id.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from opentelemetry import trace

_RUN_ID: ContextVar[str | None] = ContextVar("log_run_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": getattr(record, "run_id", None) or _RUN_ID.get(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


@contextmanager
def log_context(run_id: object) -> Iterator[None]:
    token = _RUN_ID.set(str(run_id))
    try:
        yield
    finally:
        _RUN_ID.reset(token)

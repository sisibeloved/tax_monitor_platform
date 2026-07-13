"""Structured span and JSON logging helpers without sensitive payload fields."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from time import monotonic
from typing import Iterator
from uuid import uuid4

from tax_risk.observability.context import current_context, observability_context


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().as_log_fields().items():
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context().as_log_fields())
        for field in ("span_name", "span_status", "duration_seconds", "component_code"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def start_span(name: str, logger: logging.Logger | None = None) -> Iterator[str]:
    selected_logger = logger or logging.getLogger("tax_risk.span")
    trace_id = current_context().trace_id or uuid4().hex
    started = monotonic()
    with observability_context(trace_id=trace_id):
        selected_logger.info("span_started", extra={"span_name": name})
        try:
            yield trace_id
        except Exception:
            selected_logger.exception(
                "span_finished",
                extra={
                    "span_name": name,
                    "span_status": "ERROR",
                    "duration_seconds": monotonic() - started,
                },
            )
            raise
        else:
            selected_logger.info(
                "span_finished",
                extra={
                    "span_name": name,
                    "span_status": "OK",
                    "duration_seconds": monotonic() - started,
                },
            )


def configure_structured_logging() -> None:
    """Install one JSON handler for application loggers in production processes."""

    target = logging.getLogger("tax_risk")
    if any(getattr(handler, "_tax_risk_json", False) for handler in target.handlers):
        return
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    setattr(handler, "_tax_risk_json", True)
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    target.propagate = True


__all__ = [
    "CorrelationFilter",
    "JsonFormatter",
    "configure_structured_logging",
    "start_span",
]

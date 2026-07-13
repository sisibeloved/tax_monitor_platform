"""Correlation context shared by HTTP requests, worker tasks, logs, and spans."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
import logging
from time import monotonic
from typing import Any, Iterator, Mapping, MutableMapping, cast
from uuid import UUID, uuid4


_HEADER_NAMES = {
    "request_id": "X-Request-ID",
    "task_id": "X-Task-ID",
    "run_id": "X-Run-ID",
    "company_id": "X-Company-ID",
    "fiscal_year": "X-Fiscal-Year",
    "period": "X-Accounting-Period",
    "trace_id": "X-Trace-ID",
}


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    request_id: str | None = None
    task_id: str | None = None
    run_id: UUID | None = None
    company_id: UUID | None = None
    fiscal_year: int | None = None
    period: str | None = None
    trace_id: str | None = None

    def as_log_fields(self) -> dict[str, str | int]:
        values: dict[str, str | int | UUID | None] = {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "company_id": self.company_id,
            "fiscal_year": self.fiscal_year,
            "period": self.period,
            "trace_id": self.trace_id,
        }
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in values.items()
            if value is not None
        }


_CURRENT_CONTEXT: ContextVar[ObservabilityContext] = ContextVar(
    "tax_risk_observability_context",
    default=ObservabilityContext(),
)


def current_context() -> ObservabilityContext:
    return _CURRENT_CONTEXT.get()


def bind_context(**values: object) -> Token[ObservabilityContext]:
    allowed = set(ObservabilityContext.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported observability context fields: {sorted(unknown)}")
    return _CURRENT_CONTEXT.set(replace(current_context(), **cast(Any, values)))


def reset_context(token: Token[ObservabilityContext]) -> None:
    _CURRENT_CONTEXT.reset(token)


@contextmanager
def observability_context(**values: object) -> Iterator[ObservabilityContext]:
    token = bind_context(**values)
    try:
        yield current_context()
    finally:
        reset_context(token)


def inject_context_headers(
    headers: MutableMapping[str, str],
    context: ObservabilityContext | None = None,
) -> MutableMapping[str, str]:
    selected = context or current_context()
    for field_name, header_name in _HEADER_NAMES.items():
        value = getattr(selected, field_name)
        if value is not None:
            headers[header_name] = str(value)
    return headers


def context_from_headers(headers: Mapping[str, object]) -> ObservabilityContext:
    normalized = {str(key).lower(): value for key, value in headers.items()}

    def optional_text(field_name: str) -> str | None:
        raw = normalized.get(_HEADER_NAMES[field_name].lower())
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    return ObservabilityContext(
        request_id=optional_text("request_id"),
        task_id=optional_text("task_id"),
        run_id=_optional_uuid(optional_text("run_id")),
        company_id=_optional_uuid(optional_text("company_id")),
        fiscal_year=_optional_year(optional_text("fiscal_year")),
        period=optional_text("period"),
        trace_id=optional_text("trace_id"),
    )


def _optional_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _optional_year(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        year = int(value)
    except ValueError:
        return None
    return year if 2000 <= year <= 9999 else None


def install_celery_context(app: object) -> None:
    """Propagate correlation headers and bind one isolated context per worker task."""

    from celery.signals import before_task_publish, task_postrun, task_prerun  # type: ignore[import-untyped]

    app_identity = id(app)

    def publish_context(
        sender: str | None = None,
        headers: MutableMapping[str, object] | None = None,
        **_kwargs: object,
    ) -> None:
        del sender
        if headers is None:
            return
        outbound = inject_context_headers({}, current_context())
        headers.update(outbound)

    def bind_task_context(
        sender: object | None = None,
        task_id: str | None = None,
        task: object | None = None,
        **_kwargs: object,
    ) -> None:
        selected = task or sender
        if selected is None or getattr(selected, "app", None) is not app:
            return
        request = getattr(selected, "request", None)
        raw_headers = getattr(request, "headers", None) or {}
        propagated = context_from_headers(raw_headers)
        token = _CURRENT_CONTEXT.set(
            replace(
                propagated,
                task_id=str(task_id or getattr(request, "id", "")) or None,
                trace_id=propagated.trace_id or uuid4().hex,
            )
        )
        setattr(request, "_tax_risk_context_token", token)
        setattr(request, "_tax_risk_task_started", monotonic())
        logging.getLogger("tax_risk.worker").info(
            "task_started",
            extra={"span_name": "celery.task"},
        )

    def clear_task_context(
        sender: object | None = None,
        task: object | None = None,
        **_kwargs: object,
    ) -> None:
        selected = task or sender
        if selected is None or getattr(selected, "app", None) is not app:
            return
        request = getattr(selected, "request", None)
        token = getattr(request, "_tax_risk_context_token", None)
        if token is not None:
            started = float(getattr(request, "_tax_risk_task_started", monotonic()))
            state = str(_kwargs.get("state", "SUCCESS"))
            logging.getLogger("tax_risk.worker").info(
                "task_finished",
                extra={
                    "span_name": "celery.task",
                    "span_status": state,
                    "duration_seconds": monotonic() - started,
                },
            )
            reset_context(token)
            delattr(request, "_tax_risk_context_token")

    before_task_publish.connect(
        publish_context,
        weak=False,
        dispatch_uid=f"tax-risk-publish-context-{app_identity}",
    )
    task_prerun.connect(
        bind_task_context,
        weak=False,
        dispatch_uid=f"tax-risk-bind-context-{app_identity}",
    )
    task_postrun.connect(
        clear_task_context,
        weak=False,
        dispatch_uid=f"tax-risk-clear-context-{app_identity}",
    )


__all__ = [
    "ObservabilityContext",
    "bind_context",
    "context_from_headers",
    "current_context",
    "inject_context_headers",
    "install_celery_context",
    "observability_context",
    "reset_context",
]

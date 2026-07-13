from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from datetime import date
from typing import Protocol, cast
from uuid import UUID

from celery import Celery, Task, chord, group  # type: ignore[import-untyped]
from celery.canvas import Signature  # type: ignore[import-untyped]
from celery.utils.time import get_exponential_backoff_interval  # type: ignore[import-untyped]

from tax_risk.domain.task_runs import TaskRunResult
from tax_risk.security.context import principal_context
from tax_risk.security.service_scope import (
    ServiceScopeTokenError,
    issue_service_scope_token,
    service_principal,
    verify_service_scope_token,
)

QUARTERLY_QUEUE = "quarterly"
RUN_COMPANY_TASK = "tax_risk.workers.quarterly_batch.run_company_quarterly"
SUMMARIZE_BATCH_TASK = "tax_risk.workers.quarterly_batch.summarize_quarterly_batch"


class QuarterlyBatchService(Protocol):
    """Application boundary used by the Celery adapter."""

    def run_company(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
        automatic_retry_pending: bool = False,
    ) -> dict[str, object]: ...

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None: ...

    def summarize(self, *, run_id: UUID) -> dict[str, object]: ...


QuarterlyBatchServiceFactory = Callable[[], QuarterlyBatchService]


def default_quarterly_service_factory() -> QuarterlyBatchService:
    """Build the production service lazily when a worker executes a task."""

    from tax_risk.application.quarterly_batches import QuarterlyBatchService as Service
    from tax_risk.persistence.repositories import UnitOfWork

    return cast(QuarterlyBatchService, Service(UnitOfWork))


def register_quarterly_tasks(
    *,
    app: Celery,
    service_factory: QuarterlyBatchServiceFactory,
) -> None:
    """Register the quarterly company task and persisted-state finalizer."""

    max_retries = int(app.conf.quarterly_task_max_retries)
    retry_backoff = int(app.conf.quarterly_task_retry_backoff_seconds)
    retry_backoff_max = int(app.conf.task_time_limit)

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        shared=False,
        name=RUN_COMPANY_TASK,
        queue=QUARTERLY_QUEUE,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        retry_jitter=True,
    )
    def run_company_quarterly(
        task: Task,
        run_company_id: str,
        company_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, object]:
        task_id = task.request.id
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
        try:
            parsed_run_company_id = UUID(run_company_id)
            with _task_scope_context(
                app,
                scope_token,
                expected_run_type="QUARTERLY",
                expected_batch_id=run_company_id,
                expected_company_id=company_id,
            ):
                service = service_factory()
                outcome = service.run_company(
                    run_company_id=parsed_run_company_id,
                    task_id=str(task_id),
                    automatic_retry_pending=(task.request.retries < max_retries),
                )
        except ServiceScopeTokenError:
            return _emergency_failed_outcome(
                run_company_id=run_company_id,
                task_id=str(task_id),
                error_code="WORKER_SCOPE_TOKEN_INVALID",
            )
        except Exception as error:
            if task.request.retries < max_retries:
                _retry_company_task(
                    task,
                    error=error,
                    retry_backoff=retry_backoff,
                    retry_backoff_max=retry_backoff_max,
                )
            return _emergency_failed_outcome(
                run_company_id=run_company_id,
                task_id=str(task_id),
            )
        if (
            outcome.get("status") == "RETRY_PENDING"
            and outcome.get("retryable") is True
            and outcome.get("task_id") == str(task_id)
            and task.request.retries < max_retries
        ):
            _retry_company_task(
                task,
                error=RuntimeError("retryable quarterly company result"),
                retry_backoff=retry_backoff,
                retry_backoff_max=retry_backoff_max,
            )
        if "run_type" in outcome:
            TaskRunResult.from_payload(outcome)
        return outcome

    @app.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=SUMMARIZE_BATCH_TASK,
        queue=QUARTERLY_QUEUE,
        autoretry_for=(Exception,),
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        retry_jitter=True,
    )
    def summarize_quarterly_batch(
        header_results: list[dict[str, object]],
        run_id: str,
        scope_token: str | None = None,
    ) -> dict[str, object]:
        # Header values reconcile only task-boundary emergencies. The DB remains authoritative.
        with _task_scope_context(
            app,
            scope_token,
            expected_run_type="QUARTERLY_SUMMARY",
            expected_batch_id=run_id,
        ):
            service = service_factory()
            parsed_run_id = UUID(run_id)
            service.reconcile_header_results(
                run_id=parsed_run_id,
                header_results=header_results,
            )
            return service.summarize(run_id=parsed_run_id)


def build_quarterly_batch_canvas(
    *,
    app: Celery,
    run_id: UUID,
    run_company_ids: Iterable[UUID],
    company_ids: Iterable[UUID] | None = None,
    summary_company_ids: Iterable[UUID] | None = None,
    scope_period: date | None = None,
    worker_scope_secret: str | None = None,
) -> Signature:
    """Build one ID-only company task per member followed by one finalizer."""

    resolved_run_company_ids = tuple(run_company_ids)
    resolved_company_ids = tuple(company_ids or ())
    resolved_summary_company_ids = tuple(summary_company_ids or resolved_company_ids)
    scoped = bool(
        resolved_company_ids or resolved_summary_company_ids or scope_period or worker_scope_secret
    )
    if scoped and (
        len(resolved_run_company_ids) != len(resolved_company_ids)
        or not resolved_summary_company_ids
        or not set(resolved_company_ids) <= set(resolved_summary_company_ids)
        or scope_period is None
        or not worker_scope_secret
    ):
        raise ValueError("signed quarterly scope requires one company per task")
    header = group(
        app.signature(
            RUN_COMPANY_TASK,
            args=(
                (str(run_company_id),)
                if not scoped
                else (
                    str(run_company_id),
                    str(resolved_company_ids[index]),
                    issue_service_scope_token(
                        secret=cast(str, worker_scope_secret),
                        queue=QUARTERLY_QUEUE,
                        run_type="QUARTERLY",
                        batch_id=str(run_company_id),
                        company_ids=frozenset({resolved_company_ids[index]}),
                        period=cast(date, scope_period),
                    ),
                )
            ),
            queue=QUARTERLY_QUEUE,
        )
        for index, run_company_id in enumerate(resolved_run_company_ids)
    )
    body: Signature = app.signature(
        SUMMARIZE_BATCH_TASK,
        args=(
            (str(run_id),)
            if not scoped
            else (
                str(run_id),
                issue_service_scope_token(
                    secret=cast(str, worker_scope_secret),
                    queue=QUARTERLY_QUEUE,
                    run_type="QUARTERLY_SUMMARY",
                    batch_id=str(run_id),
                    company_ids=frozenset(resolved_summary_company_ids),
                    period=cast(date, scope_period),
                ),
            )
        ),
        queue=QUARTERLY_QUEUE,
    )
    return cast(Signature, chord(header, body))


def _task_scope_context(
    app: Celery,
    token: str | None,
    *,
    expected_run_type: str,
    expected_batch_id: str,
    expected_company_id: str | None = None,
) -> AbstractContextManager[object]:
    if token is None:
        if str(app.conf.runtime_environment) == "production":
            raise ServiceScopeTokenError("signed service scope token is required in production")
        return nullcontext()
    scope = verify_service_scope_token(
        token,
        secret=str(app.conf.worker_scope_secret),
        expected_queue=QUARTERLY_QUEUE,
        expected_run_type=expected_run_type,
        expected_batch_id=expected_batch_id,
    )
    if expected_company_id is not None and scope.company_ids != frozenset(
        {UUID(expected_company_id)}
    ):
        raise ServiceScopeTokenError("quarterly task company does not match its signed scope")
    return principal_context(service_principal(scope))


def _retry_company_task(
    task: Task,
    *,
    error: Exception,
    retry_backoff: int,
    retry_backoff_max: int,
) -> None:
    countdown = get_exponential_backoff_interval(
        factor=retry_backoff,
        retries=task.request.retries,
        maximum=retry_backoff_max,
        full_jitter=True,
    )
    raise task.retry(exc=error, countdown=countdown)


def _emergency_failed_outcome(
    *,
    run_company_id: str,
    task_id: str,
    error_code: str = "CELERY_TASK_EXECUTION_FAILED",
) -> dict[str, object]:
    return {
        "run_company_id": run_company_id,
        "status": "FAILED",
        "retryable": False,
        "task_id": task_id,
        "detection_ids": [],
        "case_ids": [],
        "error_code": error_code,
    }


__all__ = [
    "QUARTERLY_QUEUE",
    "RUN_COMPANY_TASK",
    "SUMMARIZE_BATCH_TASK",
    "build_quarterly_batch_canvas",
    "default_quarterly_service_factory",
    "register_quarterly_tasks",
]

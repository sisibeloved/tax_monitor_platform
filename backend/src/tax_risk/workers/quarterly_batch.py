from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, cast
from uuid import UUID

from celery import Celery, Task, chord, group  # type: ignore[import-untyped]
from celery.canvas import Signature  # type: ignore[import-untyped]
from celery.utils.time import get_exponential_backoff_interval  # type: ignore[import-untyped]

QUARTERLY_QUEUE = "quarterly"
RUN_COMPANY_TASK = "tax_risk.workers.quarterly_batch.run_company_quarterly"
SUMMARIZE_BATCH_TASK = "tax_risk.workers.quarterly_batch.summarize_quarterly_batch"


class QuarterlyBatchService(Protocol):
    """Application boundary used by the Celery adapter."""

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]: ...

    def prepare_automatic_retry(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
    ) -> bool: ...

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
    ) -> dict[str, object]:
        task_id = task.request.id
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
        try:
            parsed_run_company_id = UUID(run_company_id)
            service = service_factory()
            outcome = service.run_company(
                run_company_id=parsed_run_company_id,
                task_id=str(task_id),
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
            outcome.get("status") == "FAILED"
            and outcome.get("retryable") is True
            and outcome.get("task_id") == str(task_id)
            and task.request.retries < max_retries
        ):
            try:
                prepared = service.prepare_automatic_retry(
                    run_company_id=parsed_run_company_id,
                    task_id=str(task_id),
                )
            except Exception as error:
                _retry_company_task(
                    task,
                    error=error,
                    retry_backoff=retry_backoff,
                    retry_backoff_max=retry_backoff_max,
                )
            if prepared:
                _retry_company_task(
                    task,
                    error=RuntimeError("retryable quarterly company result"),
                    retry_backoff=retry_backoff,
                    retry_backoff_max=retry_backoff_max,
                )
        return outcome

    @app.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=SUMMARIZE_BATCH_TASK,
        queue=QUARTERLY_QUEUE,
        autoretry_for=(Exception,),
        max_retries=None,
        retry_backoff=retry_backoff,
        retry_jitter=True,
    )
    def summarize_quarterly_batch(
        header_results: list[dict[str, object]],
        run_id: str,
    ) -> dict[str, object]:
        # Header values reconcile only task-boundary emergencies. The DB remains authoritative.
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
) -> Signature:
    """Build one ID-only company task per member followed by one finalizer."""

    header = group(
        app.signature(
            RUN_COMPANY_TASK,
            args=(str(run_company_id),),
            queue=QUARTERLY_QUEUE,
        )
        for run_company_id in run_company_ids
    )
    body: Signature = app.signature(
        SUMMARIZE_BATCH_TASK,
        args=(str(run_id),),
        queue=QUARTERLY_QUEUE,
    )
    return cast(Signature, chord(header, body))


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
) -> dict[str, object]:
    return {
        "run_company_id": run_company_id,
        "status": "FAILED",
        "retryable": False,
        "task_id": task_id,
        "detection_ids": [],
        "case_ids": [],
        "error_code": "CELERY_TASK_EXECUTION_FAILED",
    }


__all__ = [
    "QUARTERLY_QUEUE",
    "RUN_COMPANY_TASK",
    "SUMMARIZE_BATCH_TASK",
    "build_quarterly_batch_canvas",
    "default_quarterly_service_factory",
    "register_quarterly_tasks",
]

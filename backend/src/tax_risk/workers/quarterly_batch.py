from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, cast
from uuid import UUID

from celery import Celery, Task, chord, group  # type: ignore[import-untyped]
from celery.canvas import Signature  # type: ignore[import-untyped]

QUARTERLY_QUEUE = "quarterly"
RUN_COMPANY_TASK = "tax_risk.workers.quarterly_batch.run_company_quarterly"
SUMMARIZE_BATCH_TASK = "tax_risk.workers.quarterly_batch.summarize_quarterly_batch"


class QuarterlyBatchService(Protocol):
    """Application boundary used by the Celery adapter."""

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]: ...

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

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        shared=False,
        name=RUN_COMPANY_TASK,
        queue=QUARTERLY_QUEUE,
        autoretry_for=(Exception,),
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
        return service_factory().run_company(
            run_company_id=UUID(run_company_id),
            task_id=str(task_id),
        )

    @app.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=SUMMARIZE_BATCH_TASK,
        queue=QUARTERLY_QUEUE,
    )
    def summarize_quarterly_batch(
        header_results: list[dict[str, object]],
        run_id: str,
    ) -> dict[str, object]:
        # Chord header values only signal completion. Persisted rows are the source of truth.
        del header_results
        return service_factory().summarize(run_id=UUID(run_id))


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


__all__ = [
    "QUARTERLY_QUEUE",
    "RUN_COMPANY_TASK",
    "SUMMARIZE_BATCH_TASK",
    "build_quarterly_batch_canvas",
    "default_quarterly_service_factory",
    "register_quarterly_tasks",
]

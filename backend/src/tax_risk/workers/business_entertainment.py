"""Lightweight, ID-only Celery adapter for monthly entertainment monitoring."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol
from uuid import UUID

from celery import Celery, Task  # type: ignore[import-untyped]

from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentRunError,
    BusinessEntertainmentRunRequest,
)
from tax_risk.observability.metrics import record_company_task

BUSINESS_ENTERTAINMENT_QUEUE = "business-entertainment"
RUN_COMPANY_TASK = "tax_risk.workers.business_entertainment.run_company_monthly"


class BusinessEntertainmentWorkerService(Protocol):
    def run_company(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        task_id: str,
    ) -> dict[str, object]: ...


ServiceFactory = Callable[[], BusinessEntertainmentWorkerService]


def default_business_entertainment_service_factory() -> BusinessEntertainmentWorkerService:
    from tax_risk.application.business_entertainment.service import (
        build_default_business_entertainment_service,
    )

    return build_default_business_entertainment_service()


def register_business_entertainment_tasks(
    *,
    app: Celery,
    service_factory: ServiceFactory,
) -> None:
    max_retries = int(app.conf.quarterly_task_max_retries)

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        shared=False,
        name=RUN_COMPANY_TASK,
        queue=BUSINESS_ENTERTAINMENT_QUEUE,
        max_retries=max_retries,
    )
    def run_company_monthly(
        task: Task,
        run_id: str,
        company_code: str,
        period_end: str,
        snapshot_set_id: str,
        rule_version_id: str,
        lexicon_version: str,
        model_version_id: str,
        prompt_version_id: str,
        case_library_version_id: str,
        account_dictionary_version_id: str,
    ) -> dict[str, object]:
        task_id = str(task.request.id or "")
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
        request: BusinessEntertainmentRunRequest | None = None
        try:
            request = BusinessEntertainmentRunRequest(
                run_id=UUID(run_id),
                company_code=company_code,
                period_end=date.fromisoformat(period_end),
                snapshot_set_id=UUID(snapshot_set_id),
                rule_version_id=rule_version_id,
                lexicon_version=lexicon_version,
                model_version_id=model_version_id,
                prompt_version_id=prompt_version_id,
                case_library_version_id=case_library_version_id,
                account_dictionary_version_id=account_dictionary_version_id,
            )
            outcome = service_factory().run_company(request, task_id=task_id)
            _record_outcome(outcome)
            return outcome
        except BusinessEntertainmentRunError as error:
            outcome = _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code=error.error_code,
                retryable=error.retryable,
            )
            _record_outcome(outcome)
            return outcome
        except Exception as error:
            if task.request.retries < max_retries:
                raise task.retry(exc=error) from error
            outcome = _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code="CELERY_TASK_EXECUTION_FAILED",
                retryable=False,
            )
            _record_outcome(outcome)
            return outcome


def _failed_outcome(
    *,
    request: BusinessEntertainmentRunRequest | None,
    raw_run_id: str,
    company_code: str,
    task_id: str,
    error_code: str,
    retryable: bool,
) -> dict[str, object]:
    return {
        "run_id": str(request.run_id) if request else raw_run_id,
        "company_code": request.company_code if request else company_code,
        "status": "FAILED",
        "retryable": retryable,
        "task_id": task_id,
        "idempotency_key": request.idempotency_key if request else None,
        "error_code": error_code,
    }


def _record_outcome(outcome: dict[str, object]) -> None:
    record_company_task(
        run_type="MONTHLY_SEMANTIC",
        monitor_type="BUSINESS_ENTERTAINMENT",
        status=str(outcome.get("status", "FAILED")),
        error_code=(
            str(outcome["error_code"])
            if outcome.get("error_code") is not None
            else None
        ),
    )


__all__ = [
    "BUSINESS_ENTERTAINMENT_QUEUE",
    "RUN_COMPANY_TASK",
    "default_business_entertainment_service_factory",
    "register_business_entertainment_tasks",
]

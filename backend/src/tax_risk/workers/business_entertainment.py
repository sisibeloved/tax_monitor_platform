"""Lightweight, ID-only Celery adapter for monthly entertainment monitoring."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import date, datetime, timezone
from typing import Protocol
from uuid import UUID

from celery import Celery, Task  # type: ignore[import-untyped]
from pydantic import ValidationError

from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentRunError,
    BusinessEntertainmentRunRequest,
)
from tax_risk.domain.task_runs import (
    TaskRunResult,
    TaskRunType,
    TaskTerminalStatus,
    bounded_retry_delay,
    is_retryable_error,
)
from tax_risk.observability.metrics import record_company_task
from tax_risk.security.context import principal_context
from tax_risk.security.service_scope import (
    ServiceScopeTokenError,
    issue_service_scope_token,
    service_principal,
    verify_service_scope_token,
)

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
    retry_base = int(app.conf.quarterly_task_retry_backoff_seconds)
    retry_maximum = int(app.conf.task_time_limit)

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
        company_list_version_id: str,
        rule_version_id: str,
        lexicon_version: str,
        model_version_id: str,
        prompt_version_id: str,
        case_library_version_id: str,
        account_dictionary_version_id: str,
        company_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, object]:
        task_id = str(task.request.id or "")
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
        started_at = datetime.now(timezone.utc)
        request: BusinessEntertainmentRunRequest | None = None
        try:
            request = BusinessEntertainmentRunRequest(
                run_id=UUID(run_id),
                company_code=company_code,
                period_end=date.fromisoformat(period_end),
                snapshot_set_id=UUID(snapshot_set_id),
                company_list_version_id=company_list_version_id,
                rule_version_id=rule_version_id,
                lexicon_version=lexicon_version,
                model_version_id=model_version_id,
                prompt_version_id=prompt_version_id,
                case_library_version_id=case_library_version_id,
                account_dictionary_version_id=account_dictionary_version_id,
            )
            with _task_scope_context(
                app,
                scope_token,
                run_id=run_id,
                company_id=company_id,
                period_end=request.period_end,
            ):
                outcome = service_factory().run_company(request, task_id=task_id)
            _record_outcome(outcome)
            return _attach_task_contract(
                outcome,
                request=request,
                started_at=started_at,
                retry_count=task.request.retries,
            )
        except BusinessEntertainmentRunError as error:
            retryable = error.retryable and is_retryable_error(error.error_code)
            if retryable and task.request.retries < max_retries:
                countdown = bounded_retry_delay(
                    task.request.retries,
                    base_seconds=retry_base,
                    maximum_seconds=retry_maximum,
                )
                raise task.retry(exc=error, countdown=countdown) from error
            outcome = _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code=error.error_code,
                retryable=retryable,
                status="FAILED" if retryable else "BLOCKED",
            )
            _record_outcome(outcome)
            assert request is not None
            return _attach_task_contract(
                outcome,
                request=request,
                started_at=started_at,
                retry_count=task.request.retries,
            )
        except ServiceScopeTokenError:
            outcome = _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code="WORKER_SCOPE_TOKEN_INVALID",
                retryable=False,
                status="BLOCKED",
            )
            _record_outcome(outcome)
            if request is None:
                return outcome
            return _attach_task_contract(
                outcome,
                request=request,
                started_at=started_at,
                retry_count=task.request.retries,
            )
        except (ValidationError, ValueError) as error:
            del error
            return _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code="INVALID_TASK_PAYLOAD",
                retryable=False,
                status="BLOCKED",
            )
        except Exception as error:
            if task.request.retries < max_retries:
                countdown = bounded_retry_delay(
                    task.request.retries,
                    base_seconds=retry_base,
                    maximum_seconds=retry_maximum,
                )
                raise task.retry(exc=error, countdown=countdown) from error
            outcome = _failed_outcome(
                request=request,
                raw_run_id=run_id,
                company_code=company_code,
                task_id=task_id,
                error_code="CELERY_TASK_EXECUTION_FAILED",
                retryable=True,
                status="FAILED",
            )
            _record_outcome(outcome)
            if request is None:
                return outcome
            return _attach_task_contract(
                outcome,
                request=request,
                started_at=started_at,
                retry_count=task.request.retries,
            )


def build_business_entertainment_task_kwargs(
    request: BusinessEntertainmentRunRequest,
    *,
    company_id: UUID,
    worker_scope_secret: str,
) -> dict[str, str]:
    """Build the only production-safe payload for the legacy dedicated worker."""

    payload = request.to_task_kwargs()
    payload.update(
        {
            "company_id": str(company_id),
            "scope_token": issue_service_scope_token(
                secret=worker_scope_secret,
                queue=BUSINESS_ENTERTAINMENT_QUEUE,
                run_type="BUSINESS_ENTERTAINMENT",
                batch_id=str(request.run_id),
                company_ids=frozenset({company_id}),
                period=request.period_end,
            ),
        }
    )
    return payload


def _task_scope_context(
    app: Celery,
    token: str | None,
    *,
    run_id: str,
    company_id: str | None,
    period_end: date,
) -> AbstractContextManager[object]:
    if token is None:
        if str(app.conf.runtime_environment) == "production":
            raise ServiceScopeTokenError("signed service scope token is required in production")
        return nullcontext()
    if company_id is None:
        raise ServiceScopeTokenError("company id is required for a signed task")
    scope = verify_service_scope_token(
        token,
        secret=str(app.conf.worker_scope_secret),
        expected_queue=BUSINESS_ENTERTAINMENT_QUEUE,
        expected_run_type="BUSINESS_ENTERTAINMENT",
        expected_batch_id=run_id,
    )
    if scope.company_ids != frozenset({UUID(company_id)}) or scope.period != period_end:
        raise ServiceScopeTokenError("business entertainment task does not match its signed scope")
    return principal_context(service_principal(scope))


def _failed_outcome(
    *,
    request: BusinessEntertainmentRunRequest | None,
    raw_run_id: str,
    company_code: str,
    task_id: str,
    error_code: str,
    retryable: bool,
    status: str,
) -> dict[str, object]:
    return {
        "run_id": str(request.run_id) if request else raw_run_id,
        "company_code": request.company_code if request else company_code,
        "status": status,
        "retryable": retryable,
        "task_id": task_id,
        "idempotency_key": request.idempotency_key if request else None,
        "error_code": error_code,
    }


def _attach_task_contract(
    outcome: dict[str, object],
    *,
    request: BusinessEntertainmentRunRequest,
    started_at: datetime,
    retry_count: int,
) -> dict[str, object]:
    finished_at = datetime.now(timezone.utc)
    status = TaskTerminalStatus(str(outcome["status"]))
    error_code = str(outcome["error_code"]) if outcome.get("error_code") else None
    contract = TaskRunResult(
        run_type=TaskRunType.MONTHLY_SEMANTIC,
        monitor_type="BUSINESS_ENTERTAINMENT",
        batch_id=request.run_id,
        company=request.company_code,
        fiscal_year=request.period_end.year,
        period=request.period_end.strftime("%Y-%m"),
        idempotency_key=request.idempotency_key,
        terminal_status=status,
        retry_count=retry_count,
        started_at=started_at,
        finished_at=finished_at,
        company_output_ready_at=(finished_at if status == TaskTerminalStatus.SUCCEEDED else None),
        error_code=error_code,
        retryable=is_retryable_error(error_code),
    )
    merged = dict(outcome)
    merged.update(contract.to_payload())
    merged["run_id"] = str(request.run_id)
    merged["task_id"] = outcome["task_id"]
    return merged


def _record_outcome(outcome: dict[str, object]) -> None:
    record_company_task(
        run_type="MONTHLY_SEMANTIC",
        monitor_type="BUSINESS_ENTERTAINMENT",
        status=str(outcome.get("status", "FAILED")),
        error_code=(str(outcome["error_code"]) if outcome.get("error_code") is not None else None),
    )


__all__ = [
    "BUSINESS_ENTERTAINMENT_QUEUE",
    "RUN_COMPANY_TASK",
    "build_business_entertainment_task_kwargs",
    "default_business_entertainment_service_factory",
    "register_business_entertainment_tasks",
]

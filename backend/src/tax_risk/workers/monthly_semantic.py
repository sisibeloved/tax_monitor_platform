"""ID-only Celery fan-out/fan-in for frozen monthly semantic runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from datetime import date
from typing import Protocol, cast
from uuid import UUID

from celery import Celery, Task, chord, group  # type: ignore[import-untyped]
from celery.canvas import Signature  # type: ignore[import-untyped]

from tax_risk.application.monthly_semantic_runs import MonthlySemanticRunService
from tax_risk.domain.task_runs import TaskRunResult, bounded_retry_delay
from tax_risk.security.context import principal_context
from tax_risk.security.service_scope import (
    ServiceScopeTokenError,
    issue_service_scope_token,
    service_principal,
    verify_service_scope_token,
)


MONTHLY_SEMANTIC_QUEUE = "monthly-semantic"
RUN_COMPANY_TASK = "tax_risk.workers.monthly_semantic.run_company"
SUMMARIZE_TASK = "tax_risk.workers.monthly_semantic.summarize"


class MonthlyWorkerService(Protocol):
    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]: ...

    def summarize(self, run_id: UUID) -> dict[str, object]: ...


ServiceFactory = Callable[[], MonthlyWorkerService]


def default_monthly_service_factory() -> MonthlyWorkerService:
    from tax_risk.api.business_entertainment_dependencies import bind_structured_model_client
    from tax_risk.application.donation.service import build_donation_service
    from tax_risk.application.semantic.account_dictionary import (
        SuggestedAccountDictionaryService,
    )
    from tax_risk.application.semantic.detection_router import SemanticCaseRouter
    from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent
    from tax_risk.application.semantic.sap_voucher_monitor import SapVoucherMonitor
    from tax_risk.application.welfare.service import build_welfare_service
    from tax_risk.config import Settings
    from tax_risk.domain.cases import MonitorType
    from tax_risk.domain.semantic.contracts import SemanticVersionSet
    from tax_risk.persistence.repositories import UnitOfWork

    settings = Settings()
    client = bind_structured_model_client(
        settings,
        credential_resolver=lambda _reference: "",
        uow_factory=UnitOfWork,
    )
    if client is None:
        raise RuntimeError("structured model client is not configured")

    class Recorder:
        def record(self, _issue: object) -> None:
            return None

    def monitor_factory(
        *,
        uow: UnitOfWork,
        monitoring_type: MonitorType,
        versions: SemanticVersionSet,
    ) -> SapVoucherMonitor:
        agent = SapVoucherAgent(
            client,
            SuggestedAccountDictionaryService(UnitOfWork),
        )
        recorder = Recorder()
        router = SemanticCaseRouter(UnitOfWork)
        if monitoring_type is MonitorType.WELFARE:
            return build_welfare_service(
                repository=uow.monthly_semantic,
                agent=agent,
                versions=versions,
                data_issue_recorder=recorder,
                router=router,
            )
        return build_donation_service(
            repository=uow.monthly_semantic,
            agent=agent,
            versions=versions,
            data_issue_recorder=recorder,
            router=router,
        )

    return MonthlySemanticRunService(UnitOfWork, monitor_factory=monitor_factory)


def register_monthly_tasks(*, app: Celery, service_factory: ServiceFactory) -> None:
    max_retries = int(app.conf.quarterly_task_max_retries)
    retry_base = int(app.conf.quarterly_task_retry_backoff_seconds)
    retry_maximum = int(app.conf.task_time_limit)

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        shared=False,
        name=RUN_COMPANY_TASK,
        queue=MONTHLY_SEMANTIC_QUEUE,
        max_retries=max_retries,
        retry_backoff=True,
        retry_jitter=True,
    )
    def run_company(
        task: Task,
        run_company_id: str,
        company_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, object]:
        task_id = str(task.request.id or "")
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
        with _task_scope_context(
            app,
            scope_token,
            expected_run_type="MONTHLY_SEMANTIC",
            expected_batch_id=run_company_id,
            expected_company_id=company_id,
        ):
            outcome = service_factory().run_company(
                run_company_id=UUID(run_company_id),
                task_id=task_id,
            )
        if (
            outcome.get("status") == "FAILED"
            and outcome.get("retryable") is True
            and task.request.retries < max_retries
        ):
            countdown = bounded_retry_delay(
                task.request.retries,
                base_seconds=retry_base,
                maximum_seconds=retry_maximum,
            )
            raise task.retry(
                exc=RuntimeError("retryable monthly company failure"),
                countdown=countdown,
            )
        if "run_type" in outcome:
            TaskRunResult.from_payload(outcome)
        return outcome

    @app.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=SUMMARIZE_TASK,
        queue=MONTHLY_SEMANTIC_QUEUE,
    )
    def summarize(
        _header: list[dict[str, object]],
        run_id: str,
        scope_token: str | None = None,
    ) -> dict[str, object]:
        with _task_scope_context(
            app,
            scope_token,
            expected_run_type="MONTHLY_SEMANTIC_SUMMARY",
            expected_batch_id=run_id,
        ):
            return service_factory().summarize(UUID(run_id))


def build_monthly_semantic_canvas(
    *,
    app: Celery,
    run_id: UUID,
    run_company_ids: Iterable[UUID],
    company_ids: Iterable[UUID] | None = None,
    summary_company_ids: Iterable[UUID] | None = None,
    scope_period: date | None = None,
    worker_scope_secret: str | None = None,
) -> Signature:
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
        raise ValueError("signed monthly scope requires one company per task")
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
                        queue=MONTHLY_SEMANTIC_QUEUE,
                        run_type="MONTHLY_SEMANTIC",
                        batch_id=str(run_company_id),
                        company_ids=frozenset({resolved_company_ids[index]}),
                        period=cast(date, scope_period),
                    ),
                )
            ),
            queue=MONTHLY_SEMANTIC_QUEUE,
        )
        for index, run_company_id in enumerate(resolved_run_company_ids)
    )
    body: Signature = app.signature(
        SUMMARIZE_TASK,
        args=(
            (str(run_id),)
            if not scoped
            else (
                str(run_id),
                issue_service_scope_token(
                    secret=cast(str, worker_scope_secret),
                    queue=MONTHLY_SEMANTIC_QUEUE,
                    run_type="MONTHLY_SEMANTIC_SUMMARY",
                    batch_id=str(run_id),
                    company_ids=frozenset(resolved_summary_company_ids),
                    period=cast(date, scope_period),
                ),
            )
        ),
        queue=MONTHLY_SEMANTIC_QUEUE,
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
        expected_queue=MONTHLY_SEMANTIC_QUEUE,
        expected_run_type=expected_run_type,
        expected_batch_id=expected_batch_id,
    )
    if expected_company_id is not None and scope.company_ids != frozenset(
        {UUID(expected_company_id)}
    ):
        raise ServiceScopeTokenError("monthly task company does not match its signed scope")
    return principal_context(service_principal(scope))


__all__ = [
    "MONTHLY_SEMANTIC_QUEUE",
    "RUN_COMPANY_TASK",
    "SUMMARIZE_TASK",
    "build_monthly_semantic_canvas",
    "default_monthly_service_factory",
    "register_monthly_tasks",
]

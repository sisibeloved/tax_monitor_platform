"""ID-only Celery fan-out/fan-in for frozen monthly semantic runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, cast
from uuid import UUID

from celery import Celery, Task, chord, group  # type: ignore[import-untyped]
from celery.canvas import Signature  # type: ignore[import-untyped]

from tax_risk.application.monthly_semantic_runs import MonthlySemanticRunService
from tax_risk.domain.task_runs import TaskRunResult, bounded_retry_delay


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
    def run_company(task: Task, run_company_id: str) -> dict[str, object]:
        task_id = str(task.request.id or "")
        if not task_id:
            raise RuntimeError("Celery did not assign a task id")
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
    def summarize(_header: list[dict[str, object]], run_id: str) -> dict[str, object]:
        return service_factory().summarize(UUID(run_id))


def build_monthly_semantic_canvas(
    *,
    app: Celery,
    run_id: UUID,
    run_company_ids: Iterable[UUID],
) -> Signature:
    header = group(
        app.signature(
            RUN_COMPANY_TASK,
            args=(str(run_company_id),),
            queue=MONTHLY_SEMANTIC_QUEUE,
        )
        for run_company_id in run_company_ids
    )
    body: Signature = app.signature(
        SUMMARIZE_TASK,
        args=(str(run_id),),
        queue=MONTHLY_SEMANTIC_QUEUE,
    )
    return cast(Signature, chord(header, body))


__all__ = [
    "MONTHLY_SEMANTIC_QUEUE",
    "RUN_COMPANY_TASK",
    "SUMMARIZE_TASK",
    "build_monthly_semantic_canvas",
    "default_monthly_service_factory",
    "register_monthly_tasks",
]

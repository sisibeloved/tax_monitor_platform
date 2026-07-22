"""Signed, ID-only Celery adapter for income-tax refund writebacks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from celery import Celery, Task  # type: ignore[import-untyped]

from tax_risk.application.refund_writebacks import (
    RefundWritebackDelivery,
    RefundWritebackDispatchItem,
)
from tax_risk.domain.task_runs import bounded_retry_delay
from tax_risk.security.context import principal_context
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal
from tax_risk.security.service_scope import (
    ServiceScopeTokenError,
    issue_service_scope_token,
    service_principal,
    verify_service_scope_token,
)


INCOME_TAX_REFUND_WRITEBACK_QUEUE = "income-tax-refund-writeback"
DELIVER_WRITEBACK_TASK = "tax_risk.workers.income_tax_refund_writebacks.deliver_writeback"
DISPATCH_PENDING_WRITEBACKS_TASK = "tax_risk.workers.income_tax_refund_writebacks.dispatch_pending"
_RUN_TYPE = "INCOME_TAX_REFUND_WRITEBACK"


class IncomeTaxRefundWritebackWorkerService(Protocol):
    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery: ...


ServiceFactory = Callable[[], IncomeTaxRefundWritebackWorkerService]


class IncomeTaxRefundWritebackDispatchService(Protocol):
    def list_dispatchable(
        self,
        *,
        limit: int = 100,
    ) -> tuple[RefundWritebackDispatchItem, ...]: ...


DispatchServiceFactory = Callable[[], IncomeTaxRefundWritebackDispatchService]


class IncomeTaxRefundWritebackWorkerError(RuntimeError):
    """Credential-safe worker failure used for Celery retry metadata."""


class _RefundWritebackDisabledError(RuntimeError):
    error_code = "LARK_REFUND_WRITEBACK_DISABLED"
    retryable = False


class _RefundWritebackCredentialsMissingError(RuntimeError):
    error_code = "LARK_REFUND_CREDENTIALS_MISSING"
    retryable = False


class _DisabledRefundWritebackSender:
    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        raise _RefundWritebackDisabledError("Lark refund writeback is disabled")


class _MisconfiguredRefundWritebackSender:
    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        raise _RefundWritebackCredentialsMissingError(
            "Lark refund writeback worker credentials are missing"
        )


class _DispatchOnlySender:
    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        raise RuntimeError("dispatch-only refund service cannot perform writeback")


@lru_cache(maxsize=1)
def default_income_tax_refund_writeback_service_factory() -> IncomeTaxRefundWritebackWorkerService:
    """Build one process-local service so HTTP and tenant-token caches are reused."""

    from tax_risk.adapters.lark.refund_base import (
        LarkRefundBaseClient,
        LarkRefundBaseConfig,
    )
    from tax_risk.application.refund_writebacks import (
        IncomeTaxRefundWritebackService,
        RefundWritebackSender,
    )
    from tax_risk.config import Settings
    from tax_risk.persistence.repositories import UnitOfWork

    settings = Settings()
    sender: RefundWritebackSender
    if not settings.lark_refund_writeback_enabled:
        sender = _DisabledRefundWritebackSender()
    elif settings.lark_refund_app_id is None or settings.lark_refund_app_secret is None:
        sender = _MisconfiguredRefundWritebackSender()
    else:
        assert settings.lark_refund_base_token is not None
        assert settings.lark_refund_table_id is not None
        assert settings.lark_refund_company_code_field_id is not None
        assert settings.lark_refund_status_field_id is not None
        sender = LarkRefundBaseClient(
            LarkRefundBaseConfig(
                base_token=settings.lark_refund_base_token,
                table_id=settings.lark_refund_table_id,
                company_code_field_id=settings.lark_refund_company_code_field_id,
                status_field_id=settings.lark_refund_status_field_id,
                app_id=settings.lark_refund_app_id.get_secret_value(),
                app_secret=settings.lark_refund_app_secret.get_secret_value(),
                api_base_url=settings.lark_refund_api_base_url,
                timeout=settings.lark_refund_timeout_seconds,
                page_size=settings.lark_refund_page_size,
            )
        )
    return IncomeTaxRefundWritebackService(
        lambda: UnitOfWork(),
        sender,
        max_retries=settings.lark_refund_max_retries,
    )


@lru_cache(maxsize=1)
def default_income_tax_refund_dispatch_service_factory() -> IncomeTaxRefundWritebackDispatchService:
    """Build a DB-only scanner; no Lark credential or HTTP client is constructed."""

    from tax_risk.application.refund_writebacks import IncomeTaxRefundWritebackService
    from tax_risk.config import Settings
    from tax_risk.persistence.repositories import UnitOfWork

    settings = Settings()
    return IncomeTaxRefundWritebackService(
        lambda: UnitOfWork(),
        _DispatchOnlySender(),
        max_retries=settings.lark_refund_max_retries,
    )


def register_income_tax_refund_writeback_tasks(
    *,
    app: Celery,
    service_factory: ServiceFactory,
    dispatch_service_factory: DispatchServiceFactory | None = None,
) -> None:
    max_retries = int(getattr(app.conf, "lark_refund_max_retries", 3))
    retry_base = int(getattr(app.conf, "quarterly_task_retry_backoff_seconds", 5))
    retry_maximum = int(getattr(app.conf, "task_time_limit", 330))
    dispatch_batch_size = int(getattr(app.conf, "lark_refund_dispatch_batch_size", 1_000))
    resolved_dispatch_service_factory = (
        dispatch_service_factory or default_income_tax_refund_dispatch_service_factory
    )

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        shared=False,
        name=DELIVER_WRITEBACK_TASK,
        queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
        max_retries=max_retries,
    )
    def deliver_writeback(
        task: Task,
        writeback_id: str,
        company_id: str,
        scope_period: str,
        scope_token: str,
    ) -> dict[str, object]:
        try:
            parsed_writeback_id = UUID(writeback_id)
            parsed_company_id = UUID(company_id)
            parsed_period = date.fromisoformat(scope_period)
        except (TypeError, ValueError) as error:
            raise ServiceScopeTokenError("refund writeback task payload is invalid") from error

        scope = verify_service_scope_token(
            scope_token,
            secret=str(app.conf.worker_scope_secret),
            expected_queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
            expected_run_type=_RUN_TYPE,
            expected_batch_id=writeback_id,
        )
        if scope.company_ids != frozenset({parsed_company_id}) or scope.period != parsed_period:
            raise ServiceScopeTokenError("refund writeback task does not match its signed scope")

        try:
            with principal_context(service_principal(scope)):
                outcome = service_factory().deliver(
                    parsed_writeback_id,
                    expected_company_id=parsed_company_id,
                )
        except ServiceScopeTokenError:
            raise
        except Exception as error:
            safe_error = IncomeTaxRefundWritebackWorkerError(
                "income-tax refund writeback worker execution failed"
            )
            if task.request.retries < max_retries:
                raise task.retry(
                    exc=safe_error,
                    countdown=bounded_retry_delay(
                        task.request.retries,
                        base_seconds=retry_base,
                        maximum_seconds=retry_maximum,
                    ),
                ) from error
            raise safe_error from None

        if outcome.retryable and task.request.retries < max_retries:
            countdown = (
                min(outcome.retry_after_seconds, retry_maximum)
                if outcome.retry_after_seconds is not None
                else bounded_retry_delay(
                    task.request.retries,
                    base_seconds=retry_base,
                    maximum_seconds=retry_maximum,
                )
            )
            raise task.retry(
                exc=IncomeTaxRefundWritebackWorkerError(
                    outcome.error_code or "income-tax refund writeback delivery failed"
                ),
                countdown=countdown,
            )
        return outcome.to_payload()

    @app.task(  # type: ignore[untyped-decorator]
        shared=False,
        name=DISPATCH_PENDING_WRITEBACKS_TASK,
        queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
    )
    def dispatch_pending() -> dict[str, object]:
        # Beat only publishes this empty task. The worker binds the narrowly used
        # group scope needed to enumerate company-specific signed delivery tasks.
        dispatch_principal = Principal(
            subject="income-tax-refund-writeback-dispatcher",
            roles=frozenset({GROUP_TAX_ROLE}),
            allowed_company_ids=frozenset(),
            organization_path="/services/income-tax-refund-writeback-dispatcher",
        )
        with principal_context(dispatch_principal):
            items = resolved_dispatch_service_factory().list_dispatchable(limit=dispatch_batch_size)
        task_ids = dispatch_refund_writebacks(
            app=app,
            items=items,
            worker_scope_secret=str(app.conf.worker_scope_secret),
        )
        return {
            "candidate_count": len(items),
            "dispatched_count": len(task_ids),
            "task_ids": task_ids,
        }


def build_refund_writeback_task_kwargs(
    item: RefundWritebackDispatchItem,
    *,
    worker_scope_secret: str,
) -> dict[str, str]:
    return {
        "writeback_id": str(item.writeback_id),
        "company_id": str(item.company_id),
        "scope_period": item.scope_period.isoformat(),
        "scope_token": issue_service_scope_token(
            secret=worker_scope_secret,
            queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
            run_type=_RUN_TYPE,
            batch_id=str(item.writeback_id),
            company_ids=frozenset({item.company_id}),
            period=item.scope_period,
        ),
    }


def dispatch_refund_writeback(
    *,
    app: Celery,
    item: RefundWritebackDispatchItem,
    worker_scope_secret: str,
) -> str:
    async_result = app.signature(
        DELIVER_WRITEBACK_TASK,
        kwargs=build_refund_writeback_task_kwargs(
            item,
            worker_scope_secret=worker_scope_secret,
        ),
    ).apply_async()
    task_id = str(async_result.id or "")
    if not task_id:
        raise RuntimeError("Celery did not assign a task id")
    return task_id


def dispatch_refund_writebacks(
    *,
    app: Celery,
    items: Sequence[RefundWritebackDispatchItem],
    worker_scope_secret: str,
) -> tuple[str, ...]:
    return tuple(
        dispatch_refund_writeback(
            app=app,
            item=item,
            worker_scope_secret=worker_scope_secret,
        )
        for item in items
    )


__all__ = [
    "DELIVER_WRITEBACK_TASK",
    "DISPATCH_PENDING_WRITEBACKS_TASK",
    "INCOME_TAX_REFUND_WRITEBACK_QUEUE",
    "IncomeTaxRefundWritebackWorkerError",
    "build_refund_writeback_task_kwargs",
    "default_income_tax_refund_dispatch_service_factory",
    "default_income_tax_refund_writeback_service_factory",
    "dispatch_refund_writeback",
    "dispatch_refund_writebacks",
    "register_income_tax_refund_writeback_tasks",
]

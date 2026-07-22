from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from celery import Celery  # type: ignore[import-untyped]
from celery.exceptions import Retry  # type: ignore[import-untyped]

from tax_risk.adapters.lark import refund_base as lark_refund_module
from tax_risk.adapters.lark.refund_base import LarkRefundBaseConfig
from tax_risk.application.refund_writebacks import (
    RefundWritebackDelivery,
    RefundWritebackDispatchItem,
)
from tax_risk.security.context import current_principal
from tax_risk.security.principal import GROUP_TAX_ROLE
from tax_risk.security.service_scope import ServiceScopeTokenError
from tax_risk.workers.income_tax_refund_writebacks import (
    DELIVER_WRITEBACK_TASK,
    DISPATCH_PENDING_WRITEBACKS_TASK,
    INCOME_TAX_REFUND_WRITEBACK_QUEUE,
    IncomeTaxRefundWritebackWorkerError,
    build_refund_writeback_task_kwargs,
    default_income_tax_refund_writeback_service_factory,
    dispatch_refund_writeback,
    dispatch_refund_writebacks,
    register_income_tax_refund_writeback_tasks,
)


class _ScopedService:
    def __init__(
        self,
        *,
        fail_once: bool = False,
        permanent_failure: bool = False,
    ) -> None:
        self.fail_once = fail_once
        self.permanent_failure = permanent_failure
        self.calls: list[tuple[UUID, UUID]] = []

    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery:
        assert expected_company_id is not None
        principal = current_principal()
        assert principal is not None and principal.is_service
        assert principal.allowed_company_ids == frozenset({expected_company_id})
        self.calls.append((writeback_id, expected_company_id))
        if self.permanent_failure:
            return RefundWritebackDelivery(
                writeback_id=writeback_id,
                company_id=expected_company_id,
                company_code="3000",
                status="FAILED",
                attempt_count=1,
                claimed=True,
                retryable=False,
                error_code="LARK_REFUND_RECORD_NOT_FOUND",
            )
        if self.fail_once and len(self.calls) == 1:
            return RefundWritebackDelivery(
                writeback_id=writeback_id,
                company_id=expected_company_id,
                company_code="3000",
                status="FAILED",
                attempt_count=1,
                claimed=True,
                retryable=True,
                error_code="REFUND_WRITEBACK_DELIVERY_FAILED:TestError",
            )
        return RefundWritebackDelivery(
            writeback_id=writeback_id,
            company_id=expected_company_id,
            company_code="3000",
            status="SUCCEEDED",
            attempt_count=len(self.calls),
            claimed=True,
            retryable=False,
        )


class _ExplodingService:
    def __init__(self) -> None:
        self.call_count = 0

    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery:
        del writeback_id, expected_company_id
        self.call_count += 1
        raise RuntimeError("AppSecret=must-not-escape-the-worker")


class _ScopeExplodingService:
    def __init__(self) -> None:
        self.call_count = 0

    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery:
        del writeback_id, expected_company_id
        self.call_count += 1
        raise ServiceScopeTokenError("nested service scope is invalid")


class _RateLimitedService:
    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery:
        return RefundWritebackDelivery(
            writeback_id=writeback_id,
            company_id=expected_company_id,
            company_code="3000",
            status="FAILED",
            attempt_count=1,
            claimed=True,
            retryable=True,
            error_code="LARK_REFUND_RATE_LIMITED",
            retry_after_seconds=999,
        )


class _DispatchService:
    def __init__(self, items: tuple[RefundWritebackDispatchItem, ...]) -> None:
        self.items = items
        self.limits: list[int] = []

    def list_dispatchable(
        self,
        *,
        limit: int = 100,
    ) -> tuple[RefundWritebackDispatchItem, ...]:
        principal = current_principal()
        assert principal is not None
        assert principal.has_role(GROUP_TAX_ROLE)
        assert principal.allowed_company_ids == frozenset()
        self.limits.append(limit)
        return self.items


class _CapturingLarkClient:
    instances: list[_CapturingLarkClient] = []

    def __init__(self, config: LarkRefundBaseConfig) -> None:
        self.config = config
        self.instances.append(self)

    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        return object()


class _MissingTaskIdSignature:
    def apply_async(self) -> object:
        return type("MissingTaskIdResult", (), {"id": None})()


class _MissingTaskIdApp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def signature(self, name: str, *, kwargs: dict[str, str]) -> _MissingTaskIdSignature:
        self.calls.append((name, kwargs))
        return _MissingTaskIdSignature()


def _app(*, max_retries: int = 3) -> Celery:
    app = Celery(
        f"refund-writeback-worker-{uuid4()}",
        broker="memory://",
        backend="cache+memory://",
    )
    app.conf.update(
        task_always_eager=True,
        task_eager_propagates=False,
        task_store_eager_result=True,
        worker_scope_secret="signed-refund-writeback-worker-test",
        lark_refund_max_retries=max_retries,
        quarterly_task_retry_backoff_seconds=1,
        task_time_limit=30,
    )
    return app


def _item() -> RefundWritebackDispatchItem:
    return RefundWritebackDispatchItem(
        writeback_id=uuid4(),
        company_id=uuid4(),
        scope_period=date(2026, 6, 30),
    )


def test_worker_verifies_scope_before_running_as_the_service_principal() -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()

    result = (
        app.signature(
            DELIVER_WRITEBACK_TASK,
            kwargs=build_refund_writeback_task_kwargs(
                item,
                worker_scope_secret=str(app.conf.worker_scope_secret),
            ),
        )
        .apply_async()
        .get(timeout=10)
    )

    assert result["status"] == "SUCCEEDED"
    assert result["writeback_id"] == str(item.writeback_id)
    assert result["company_id"] == str(item.company_id)
    assert service.calls == [(item.writeback_id, item.company_id)]


def test_worker_rejects_a_tampered_scope_before_service_access() -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()
    kwargs = build_refund_writeback_task_kwargs(
        item,
        worker_scope_secret=str(app.conf.worker_scope_secret),
    )
    kwargs["company_id"] = str(uuid4())

    with pytest.raises(ServiceScopeTokenError):
        app.signature(DELIVER_WRITEBACK_TASK, kwargs=kwargs).apply_async().get(timeout=10)

    assert service.calls == []


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("writeback_id", "not-a-uuid"),
        ("company_id", "not-a-uuid"),
        ("scope_period", "2026-13-40"),
    ],
)
def test_worker_rejects_invalid_id_or_date_before_service_access(
    field: str,
    invalid_value: str,
) -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    kwargs = build_refund_writeback_task_kwargs(
        _item(),
        worker_scope_secret=str(app.conf.worker_scope_secret),
    )
    kwargs[field] = invalid_value

    with pytest.raises(ServiceScopeTokenError, match="task payload is invalid"):
        app.signature(DELIVER_WRITEBACK_TASK, kwargs=kwargs).apply_async().get(timeout=10)

    assert service.calls == []


def test_worker_rejects_a_tampered_signature_before_service_access() -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    kwargs = build_refund_writeback_task_kwargs(
        _item(),
        worker_scope_secret=str(app.conf.worker_scope_secret),
    )
    kwargs["scope_token"] = f"{kwargs['scope_token']}x"

    with pytest.raises(ServiceScopeTokenError, match="signature is invalid"):
        app.signature(DELIVER_WRITEBACK_TASK, kwargs=kwargs).apply_async().get(timeout=10)

    assert service.calls == []


def test_worker_rejects_a_period_that_does_not_match_its_signed_scope() -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    kwargs = build_refund_writeback_task_kwargs(
        _item(),
        worker_scope_secret=str(app.conf.worker_scope_secret),
    )
    kwargs["scope_period"] = "2026-07-31"

    with pytest.raises(ServiceScopeTokenError, match="does not match its signed scope"):
        app.signature(DELIVER_WRITEBACK_TASK, kwargs=kwargs).apply_async().get(timeout=10)

    assert service.calls == []


def test_retryable_delivery_is_retried_with_the_same_signed_scope() -> None:
    app = _app(max_retries=1)
    service = _ScopedService(fail_once=True)
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()

    result = (
        app.signature(
            DELIVER_WRITEBACK_TASK,
            kwargs=build_refund_writeback_task_kwargs(
                item,
                worker_scope_secret=str(app.conf.worker_scope_secret),
            ),
        )
        .apply_async()
        .get(timeout=10)
    )

    assert result["status"] == "SUCCEEDED"
    assert service.calls == [
        (item.writeback_id, item.company_id),
        (item.writeback_id, item.company_id),
    ]


def test_rate_limit_hint_controls_retry_countdown_with_worker_side_cap() -> None:
    app = _app(max_retries=1)
    app.conf.task_eager_propagates = True
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=_RateLimitedService,
    )
    item = _item()

    with pytest.raises(Retry) as raised:
        app.signature(
            DELIVER_WRITEBACK_TASK,
            kwargs=build_refund_writeback_task_kwargs(
                item,
                worker_scope_secret=str(app.conf.worker_scope_secret),
            ),
        ).apply_async()

    assert raised.value.when == 30


def test_nonretryable_delivery_returns_after_one_attempt() -> None:
    app = _app(max_retries=3)
    service = _ScopedService(permanent_failure=True)
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()

    result = (
        app.signature(
            DELIVER_WRITEBACK_TASK,
            kwargs=build_refund_writeback_task_kwargs(
                item,
                worker_scope_secret=str(app.conf.worker_scope_secret),
            ),
        )
        .apply_async()
        .get(timeout=10)
    )

    assert result["status"] == "FAILED"
    assert result["retryable"] is False
    assert result["error_code"] == "LARK_REFUND_RECORD_NOT_FOUND"
    assert service.calls == [(item.writeback_id, item.company_id)]


def test_service_exception_retries_then_raises_a_credential_safe_error() -> None:
    app = _app(max_retries=1)
    service = _ExplodingService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()

    eager_result = app.signature(
        DELIVER_WRITEBACK_TASK,
        kwargs=build_refund_writeback_task_kwargs(
            item,
            worker_scope_secret=str(app.conf.worker_scope_secret),
        ),
    ).apply_async()

    with pytest.raises(
        IncomeTaxRefundWritebackWorkerError,
        match="income-tax refund writeback worker execution failed",
    ) as captured:
        eager_result.get(timeout=10)

    assert service.call_count == 2
    assert "AppSecret" not in str(captured.value)


def test_nested_service_scope_error_is_not_wrapped_or_retried() -> None:
    app = _app(max_retries=3)
    service = _ScopeExplodingService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    item = _item()

    result = app.signature(
        DELIVER_WRITEBACK_TASK,
        kwargs=build_refund_writeback_task_kwargs(
            item,
            worker_scope_secret=str(app.conf.worker_scope_secret),
        ),
    ).apply_async()

    with pytest.raises(ServiceScopeTokenError, match="nested service scope is invalid"):
        result.get(timeout=10)
    assert service.call_count == 1


def test_batch_dispatch_enqueues_each_item_with_its_own_scope() -> None:
    app = _app()
    service = _ScopedService()
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
    )
    items = (_item(), _item())

    task_ids = dispatch_refund_writebacks(
        app=app,
        items=items,
        worker_scope_secret=str(app.conf.worker_scope_secret),
    )

    assert len(task_ids) == 2
    assert all(task_id for task_id in task_ids)
    assert service.calls == [
        (items[0].writeback_id, items[0].company_id),
        (items[1].writeback_id, items[1].company_id),
    ]


def test_periodic_dispatch_scans_without_lark_credentials_and_signs_each_delivery() -> None:
    app = _app()
    service = _ScopedService()
    items = (_item(), _item())
    dispatch_service = _DispatchService(items)
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=lambda: service,
        dispatch_service_factory=lambda: dispatch_service,
    )

    result = app.signature(DISPATCH_PENDING_WRITEBACKS_TASK).apply_async().get(timeout=10)

    assert result["candidate_count"] == 2
    assert result["dispatched_count"] == 2
    assert len(result["task_ids"]) == 2
    assert dispatch_service.limits == [1_000]
    assert service.calls == [
        (items[0].writeback_id, items[0].company_id),
        (items[1].writeback_id, items[1].company_id),
    ]


def test_dispatch_rejects_a_broker_result_without_a_task_id() -> None:
    app = _MissingTaskIdApp()
    item = _item()

    with pytest.raises(RuntimeError, match="did not assign a task id"):
        dispatch_refund_writeback(
            app=cast(Celery, app),
            item=item,
            worker_scope_secret="signed-refund-writeback-worker-test",
        )

    assert len(app.calls) == 1
    task_name, kwargs = app.calls[0]
    assert task_name == DELIVER_WRITEBACK_TASK
    assert kwargs["writeback_id"] == str(item.writeback_id)
    assert kwargs["company_id"] == str(item.company_id)


def test_disabled_default_factory_uses_a_permanent_failing_sender(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LARK_REFUND_WRITEBACK_ENABLED", "false")
    default_income_tax_refund_writeback_service_factory.cache_clear()
    try:
        service = default_income_tax_refund_writeback_service_factory()
        sender = getattr(service, "_sender")

        with pytest.raises(RuntimeError, match="writeback is disabled") as captured:
            sender.write_status("3000", "已退税")

        assert getattr(captured.value, "error_code") == "LARK_REFUND_WRITEBACK_DISABLED"
        assert getattr(captured.value, "retryable") is False
    finally:
        default_income_tax_refund_writeback_service_factory.cache_clear()


def test_enabled_default_factory_uses_a_permanent_error_for_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LARK_REFUND_WRITEBACK_ENABLED", "true")
    monkeypatch.delenv("LARK_REFUND_APP_ID", raising=False)
    monkeypatch.delenv("LARK_REFUND_APP_SECRET", raising=False)
    default_income_tax_refund_writeback_service_factory.cache_clear()
    try:
        service = default_income_tax_refund_writeback_service_factory()
        sender = getattr(service, "_sender")

        with pytest.raises(RuntimeError, match="credentials are missing") as captured:
            sender.write_status("3000", "已退税")

        assert getattr(captured.value, "error_code") == "LARK_REFUND_CREDENTIALS_MISSING"
        assert getattr(captured.value, "retryable") is False
    finally:
        default_income_tax_refund_writeback_service_factory.cache_clear()


def test_enabled_default_factory_maps_credentials_and_base_schema_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    configured = {
        "LARK_REFUND_WRITEBACK_ENABLED": "true",
        "LARK_REFUND_BASE_URL": "https://feishu.example.test/base/test-base",
        "LARK_REFUND_API_BASE_URL": "https://open.feishu.cn",
        "LARK_REFUND_BASE_TOKEN": "test-base",
        "LARK_REFUND_TABLE_ID": "test-table",
        "LARK_REFUND_COMPANY_CODE_FIELD_ID": "company-field",
        "LARK_REFUND_STATUS_FIELD_ID": "status-field",
        "LARK_REFUND_APP_ID": "test-app-id",
        "LARK_REFUND_APP_SECRET": "test-app-secret",
        "LARK_REFUND_TIMEOUT_SECONDS": "17",
        "LARK_REFUND_PAGE_SIZE": "77",
        "LARK_REFUND_MAX_RETRIES": "2",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    _CapturingLarkClient.instances.clear()
    monkeypatch.setattr(
        lark_refund_module,
        "LarkRefundBaseClient",
        _CapturingLarkClient,
    )
    default_income_tax_refund_writeback_service_factory.cache_clear()
    try:
        service = default_income_tax_refund_writeback_service_factory()

        assert len(_CapturingLarkClient.instances) == 1
        client = _CapturingLarkClient.instances[0]
        assert getattr(service, "_sender") is client
        assert getattr(service, "_max_retries") == 2
        assert client.config.base_token == "test-base"
        assert client.config.table_id == "test-table"
        assert client.config.company_code_field_id == "company-field"
        assert client.config.status_field_id == "status-field"
        assert client.config.app_id == "test-app-id"
        assert client.config.app_secret == "test-app-secret"
        assert client.config.api_base_url == "https://open.feishu.cn"
        assert client.config.timeout == 17
        assert client.config.page_size == 77
    finally:
        default_income_tax_refund_writeback_service_factory.cache_clear()


def test_production_celery_app_registers_and_routes_the_writeback_task() -> None:
    from tax_risk.workers.celery_app import celery_app

    assert DELIVER_WRITEBACK_TASK in celery_app.tasks
    assert celery_app.conf.task_routes[DELIVER_WRITEBACK_TASK] == {
        "queue": INCOME_TAX_REFUND_WRITEBACK_QUEUE
    }
    assert DISPATCH_PENDING_WRITEBACKS_TASK in celery_app.tasks
    assert celery_app.conf.task_routes[DISPATCH_PENDING_WRITEBACKS_TASK] == {
        "queue": INCOME_TAX_REFUND_WRITEBACK_QUEUE
    }
    assert celery_app.conf.lark_refund_max_retries == 3


def test_beat_schedule_is_enabled_without_requiring_lark_credentials() -> None:
    from tax_risk.config import Settings
    from tax_risk.workers.celery_app import create_celery_app

    disabled = create_celery_app(Settings(lark_refund_writeback_enabled=False))
    enabled = create_celery_app(
        Settings(
            lark_refund_writeback_enabled=True,
            lark_refund_base_url="https://feishu.example.test/base/refund-base",
            lark_refund_api_base_url="https://open.feishu.cn",
            lark_refund_base_token="refund-base",
            lark_refund_table_id="refund-table",
            lark_refund_company_code_field_id="company-field",
            lark_refund_status_field_id="status-field",
            lark_refund_app_id=None,
            lark_refund_app_secret=None,
        )
    )

    assert disabled.conf.beat_schedule == {}
    assert enabled.conf.beat_schedule == {
        "dispatch-pending-income-tax-refund-writebacks": {
            "task": DISPATCH_PENDING_WRITEBACKS_TASK,
            "schedule": 60.0,
            "options": {"queue": INCOME_TAX_REFUND_WRITEBACK_QUEUE},
        }
    }

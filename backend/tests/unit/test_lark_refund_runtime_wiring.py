from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import ClassVar, Protocol, cast
from uuid import UUID

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from tax_risk import main as main_module
from tax_risk.api.income_tax_refund_schemas import IncomeTaxRefundScanRequest
from tax_risk.api.routes import income_tax_refunds as refund_routes
from tax_risk.application import refund_writebacks as refund_writeback_module
from tax_risk.application.income_tax_refunds import (
    IncomeTaxRefundService,
    IncomeTaxRefundServiceError,
    IncomeTaxRefundSummaryView,
)
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal
from tax_risk.workers import celery_app as celery_module
from tax_risk.workers import income_tax_refund_writebacks as writeback_worker_module


def _enabled_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "lark_refund_writeback_enabled": True,
        "lark_refund_base_url": "https://feishu.example.test/base/refund-base",
        "lark_refund_api_base_url": "https://open.feishu.cn",
        "lark_refund_base_token": "refund-base",
        "lark_refund_table_id": "refund-table",
        "lark_refund_company_code_field_id": "company-code-field",
        "lark_refund_status_field_id": "status-field",
        "worker_scope_secret": "unit-refund-worker-scope-secret",
        "lark_refund_max_retries": 7,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_create_app_binds_a_noop_dispatcher_when_writeback_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unexpected_calls: list[object] = []

    def unexpected_default_dispatch(**kwargs: object) -> tuple[str, ...]:
        unexpected_calls.append(kwargs)
        return ("unexpected",)

    monkeypatch.setattr(
        main_module,
        "_dispatch_income_tax_refund_writebacks",
        unexpected_default_dispatch,
    )

    app = main_module.create_app(settings=Settings(lark_refund_writeback_enabled=False))

    assert app.state.income_tax_refund_writeback_dispatcher() == ()
    assert unexpected_calls == []


def test_create_app_binds_and_invokes_the_enabled_default_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    uow_factory = _unused_uow_factory

    def fake_default_dispatch(**kwargs: object) -> tuple[str, ...]:
        calls.append(kwargs)
        return ("task-1",)

    monkeypatch.setattr(
        main_module,
        "_dispatch_income_tax_refund_writebacks",
        fake_default_dispatch,
    )
    settings = _enabled_settings()

    app = main_module.create_app(settings=settings, uow_factory=uow_factory)
    result = app.state.income_tax_refund_writeback_dispatcher()

    assert result == ("task-1",)
    assert calls == [
        {
            "uow_factory": uow_factory,
            "worker_scope_secret": "unit-refund-worker-scope-secret",
            "max_retries": 7,
        }
    ]


def test_create_app_prefers_an_injected_dispatcher_even_when_writeback_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_calls: list[object] = []
    custom_calls: list[str] = []

    def default_dispatch(**kwargs: object) -> tuple[str, ...]:
        default_calls.append(kwargs)
        return ("default",)

    def custom_dispatch() -> tuple[str, ...]:
        custom_calls.append("custom")
        return ("custom",)

    monkeypatch.setattr(
        main_module,
        "_dispatch_income_tax_refund_writebacks",
        default_dispatch,
    )

    app = main_module.create_app(
        settings=_enabled_settings(),
        income_tax_refund_writeback_dispatcher=custom_dispatch,
    )

    assert app.state.income_tax_refund_writeback_dispatcher is custom_dispatch
    assert app.state.income_tax_refund_writeback_dispatcher() == ("custom",)
    assert custom_calls == ["custom"]
    assert default_calls == []


class _FakeWritebackService:
    instances: ClassVar[list[_FakeWritebackService]] = []

    def __init__(
        self,
        uow_factory: object,
        sender: object,
        *,
        max_retries: int,
    ) -> None:
        self.uow_factory = uow_factory
        self.sender = sender
        self.max_retries = max_retries
        self.limits: list[int] = []
        self.instances.append(self)

    def list_dispatchable(self, *, limit: int) -> tuple[str, ...]:
        self.limits.append(limit)
        return ("pending-item",)


class _WriteStatusSender(Protocol):
    def write_status(self, company_code: str, desired_value: str) -> object: ...


def test_default_dispatcher_constructs_the_service_and_publishes_its_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeWritebackService.instances.clear()
    uow_factory = _unused_uow_factory
    fake_celery_app = object()
    publish_calls: list[dict[str, object]] = []

    def fake_publish(**kwargs: object) -> tuple[str, ...]:
        publish_calls.append(kwargs)
        return ("task-a", "task-b")

    monkeypatch.setattr(
        refund_writeback_module,
        "IncomeTaxRefundWritebackService",
        _FakeWritebackService,
    )
    monkeypatch.setattr(celery_module, "celery_app", fake_celery_app)
    monkeypatch.setattr(
        writeback_worker_module,
        "dispatch_refund_writebacks",
        fake_publish,
    )

    result = main_module._dispatch_income_tax_refund_writebacks(
        uow_factory=uow_factory,
        worker_scope_secret="signed-worker-scope",
        max_retries=4,
    )

    assert result == ("task-a", "task-b")
    assert len(_FakeWritebackService.instances) == 1
    service = _FakeWritebackService.instances[0]
    assert service.uow_factory is uow_factory
    assert service.max_retries == 4
    assert service.limits == [1_000]
    with pytest.raises(RuntimeError, match="dispatch-only"):
        cast(_WriteStatusSender, service.sender).write_status("3000", "已退税")
    assert publish_calls == [
        {
            "app": fake_celery_app,
            "items": ("pending-item",),
            "worker_scope_secret": "signed-worker-scope",
        }
    ]


class _StubScanService:
    def __init__(
        self,
        *,
        view: IncomeTaxRefundSummaryView | None = None,
        error: IncomeTaxRefundServiceError | None = None,
    ) -> None:
        self.view = view
        self.error = error
        self.calls: list[dict[str, object]] = []

    def scan(
        self,
        *,
        refund_tax_year: int,
        scan_year: int,
        scan_month: int,
        source_batch_key: str,
        allowed_company_ids: frozenset[UUID] | None = None,
    ) -> IncomeTaxRefundSummaryView:
        self.calls.append(
            {
                "refund_tax_year": refund_tax_year,
                "scan_year": scan_year,
                "scan_month": scan_month,
                "source_batch_key": source_batch_key,
                "allowed_company_ids": allowed_company_ids,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.view is not None
        return self.view


class _DispatchFailure(RuntimeError):
    pass


def test_scan_route_dispatches_only_after_a_successful_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubScanService(view=_empty_view())
    dispatch_calls: list[str] = []
    request = _request(lambda: dispatch_calls.append("dispatch"))
    monkeypatch.setattr(refund_routes, "_service", lambda _request: _as_service(service))

    response = refund_routes.run_scan(_scan_request(), request, _group_principal())

    assert response.refund_tax_year == 2025
    assert response.scan_period == "2026-03-31"
    assert dispatch_calls == ["dispatch"]
    assert service.calls == [
        {
            "refund_tax_year": 2025,
            "scan_year": 2026,
            "scan_month": 3,
            "source_batch_key": "sap-refund-march",
            "allowed_company_ids": None,
        }
    ]
    assert request.state.audit_row_count == 0


def test_scan_route_does_not_dispatch_when_the_service_rejects_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubScanService(
        error=IncomeTaxRefundServiceError(
            "SAP_EVIDENCE_INCOMPLETE",
            "evidence is incomplete",
        )
    )
    dispatch_calls: list[str] = []
    request = _request(lambda: dispatch_calls.append("dispatch"))
    monkeypatch.setattr(refund_routes, "_service", lambda _request: _as_service(service))

    with pytest.raises(HTTPException) as raised:
        refund_routes.run_scan(_scan_request(), request, _group_principal())

    assert raised.value.status_code == 409
    assert cast(dict[str, str], raised.value.detail) == {
        "code": "SAP_EVIDENCE_INCOMPLETE",
        "message": "evidence is incomplete",
    }
    assert dispatch_calls == []


def test_scan_route_propagates_dispatch_failure_after_the_scan_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubScanService(view=_empty_view())

    def failing_dispatch() -> object:
        raise _DispatchFailure("broker unavailable")

    request = _request(failing_dispatch)
    monkeypatch.setattr(refund_routes, "_service", lambda _request: _as_service(service))

    with pytest.raises(_DispatchFailure, match="broker unavailable"):
        refund_routes.run_scan(_scan_request(), request, _group_principal())

    assert len(service.calls) == 1
    assert not hasattr(request.state, "audit_row_count")


def _as_service(service: _StubScanService) -> IncomeTaxRefundService:
    return cast(IncomeTaxRefundService, service)


def _unused_uow_factory() -> UnitOfWork:
    raise AssertionError("unit wiring tests must not open a database unit of work")


def _request(dispatcher: Callable[[], object]) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(income_tax_refund_writeback_dispatcher=dispatcher)
    )
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/v1/income-tax-refunds/scans",
            "headers": [],
        }
    )


def _scan_request() -> IncomeTaxRefundScanRequest:
    return IncomeTaxRefundScanRequest(
        refund_tax_year=2025,
        scan_year=2026,
        scan_month=3,
        source_batch_key=" sap-refund-march ",
    )


def _group_principal() -> Principal:
    return Principal(
        subject="group-tax@example.test",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/GROUP/TAX",
    )


def _empty_view() -> IncomeTaxRefundSummaryView:
    return IncomeTaxRefundSummaryView(
        refund_tax_year=2025,
        scan_period="2026-03-31",
        received_count=0,
        not_received_count=0,
        wrong_account_count=0,
        ambiguous_count=0,
        received=(),
        not_received=(),
        ambiguous=(),
    )

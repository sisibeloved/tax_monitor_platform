from collections.abc import Awaitable, Callable
import logging
from fastapi import FastAPI, Request, Response
from threading import BoundedSemaphore
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from tax_risk.api.routes.health import router as health_router
from tax_risk.api.routes.audit import router as audit_router
from tax_risk.api.routes.ingest import router as ingest_router
from tax_risk.api.routes.cases import router as cases_router
from tax_risk.api.routes.dashboard import router as dashboard_router
from tax_risk.api.routes.master_data import router as master_data_router
from tax_risk.api.routes.monthly_semantic import router as monthly_semantic_router
from tax_risk.api.routes.runs import router as runs_router
from tax_risk.api.routes.snapshots import router as snapshots_router
from tax_risk.api.routes.semantic_governance import router as semantic_governance_router
from tax_risk.api.routes.exports import router as exports_router
from tax_risk.api.routes.business_entertainment import (
    router as business_entertainment_router,
)
from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits
from tax_risk.application.ingest import (
    AdapterFactory,
    UowFactory,
    create_csv_adapter,
)
from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.application.audit import AuditEventDraft, AuditService, normalized_filter_hash
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import PrincipalProvider
from tax_risk.api.business_entertainment_dependencies import (
    bind_structured_model_client,
)


logger = logging.getLogger(__name__)


def create_app(
    *,
    uow_factory: UowFactory | None = None,
    adapter_factory: AdapterFactory | None = None,
    settings: Settings | None = None,
    principal_provider: PrincipalProvider | None = None,
    quarterly_dispatcher: Callable[..., None] | None = None,
    monthly_semantic_dispatcher: Callable[..., None] | None = None,
    semantic_credential_resolver: Callable[[str], str] | None = None,
) -> FastAPI:
    """Create the tax risk monitoring API."""

    resolved_settings = settings or Settings()
    app = FastAPI(title="Group Income Tax Risk Monitoring Platform")
    app.state.uow_factory = uow_factory or UnitOfWork
    app.state.audit_service = AuditService(app.state.uow_factory)
    app.state.settings = resolved_settings
    app.state.principal_provider = principal_provider
    app.state.adapter_factory = adapter_factory or create_csv_adapter
    app.state.quarterly_batch_service_factory = lambda: QuarterlyBatchService(
        app.state.uow_factory
    )
    app.state.quarterly_dispatcher = quarterly_dispatcher or _dispatch_quarterly_batch
    app.state.monthly_semantic_dispatcher = (
        monthly_semantic_dispatcher or _dispatch_monthly_semantic_batch
    )
    app.state.structured_model_client = bind_structured_model_client(
        resolved_settings,
        credential_resolver=semantic_credential_resolver or (lambda _reference: ""),
        uow_factory=app.state.uow_factory,
    )
    app.state.ingest_max_upload_bytes = resolved_settings.ingest_max_upload_bytes
    app.state.ingest_upload_semaphore = BoundedSemaphore(
        resolved_settings.ingest_max_concurrent_uploads
    )
    app.state.tax_master_xlsx_limits = XlsxResourceLimits(
        max_zip_members=resolved_settings.tax_master_xlsx_max_zip_members,
        max_total_uncompressed_bytes=(
            resolved_settings.tax_master_xlsx_max_total_uncompressed_bytes
        ),
        max_member_uncompressed_bytes=(
            resolved_settings.tax_master_xlsx_max_member_uncompressed_bytes
        ),
        max_compression_ratio=resolved_settings.tax_master_xlsx_max_compression_ratio,
        max_worksheet_rows=resolved_settings.tax_master_xlsx_max_worksheet_rows,
        max_worksheet_cells=resolved_settings.tax_master_xlsx_max_worksheet_cells,
    )

    @app.middleware("http")
    async def immutable_audit_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            _append_http_audit(request, 500, app.state.audit_service, request_id)
            raise
        _append_http_audit(
            request,
            response.status_code,
            app.state.audit_service,
            request_id,
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(health_router)
    app.include_router(audit_router)
    app.include_router(ingest_router)
    app.include_router(cases_router)
    app.include_router(dashboard_router)
    app.include_router(master_data_router)
    app.include_router(snapshots_router)
    app.include_router(runs_router)
    app.include_router(semantic_governance_router)
    app.include_router(exports_router)
    app.include_router(business_entertainment_router)
    app.include_router(monthly_semantic_router)
    return app


def _append_http_audit(
    request: Request,
    status_code: int,
    service: AuditService,
    request_id: str,
) -> None:
    action = _http_audit_action(request.method, request.url.path)
    if action is None:
        return
    principal = getattr(request.state, "principal", None)
    company_ids = frozenset(
        getattr(
            request.state,
            "audit_company_ids",
            principal.allowed_company_ids if principal is not None else (),
        )
    )
    filters: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        filters.setdefault(key, []).append(value)
    target_id = _target_id(request.url.path, request.method)
    result = (
        "SUCCEEDED"
        if status_code < 400
        else "DENIED"
        if status_code in {401, 403, 404}
        else "FAILED"
    )
    try:
        service.append(
            AuditEventDraft(
                action=action,
                entity_type=_entity_type(request.url.path),
                entity_id=target_id,
                principal=principal,
                company_ids=company_ids,
                result=result,
                request_id=request_id,
                filters_hash=normalized_filter_hash(filters),
                row_count=getattr(request.state, "audit_row_count", None),
                reason_code=None if result == "SUCCEEDED" else f"HTTP_{status_code}",
                payload={"method": request.method, "path": request.url.path},
            )
        )
    except Exception:
        logger.exception("security_audit_append_failed", extra={"request_id": request_id})
        if status_code < 500:
            raise


def _http_audit_action(method: str, path: str) -> str | None:
    if not path.startswith("/api/v1/") or path.startswith("/api/v1/audit-events"):
        return None
    if path == "/api/v1/risk-cases" and method == "GET":
        return "HTTP_RISK_CASE_LIST"
    if path.startswith("/api/v1/risk-cases/"):
        return "HTTP_RISK_CASE_ACTION" if method != "GET" else "HTTP_RISK_CASE_DETAIL"
    normalized = path.removeprefix("/api/v1/").replace("/", "_").replace("-", "_")
    return f"HTTP_{method}_{normalized}".upper()[:128]


def _entity_type(path: str) -> str:
    segment = path.removeprefix("/api/v1/").split("/", 1)[0]
    return segment.replace("-", "_").upper()[:128]


def _target_id(path: str, method: str) -> UUID:
    for segment in reversed(path.rstrip("/").split("/")):
        try:
            return UUID(segment)
        except ValueError:
            continue
    return uuid5(NAMESPACE_URL, f"{method}:{path}")


def _dispatch_quarterly_batch(
    *,
    run_id: UUID,
    run_company_ids: tuple[UUID, ...],
) -> None:
    """Send the durable ID-only quarterly canvas through the production broker."""

    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.quarterly_batch import build_quarterly_batch_canvas

    build_quarterly_batch_canvas(
        app=celery_app,
        run_id=run_id,
        run_company_ids=run_company_ids,
    ).apply_async()


def _dispatch_monthly_semantic_batch(
    *,
    run_id: UUID,
    run_company_ids: tuple[UUID, ...],
) -> None:
    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.monthly_semantic import build_monthly_semantic_canvas

    build_monthly_semantic_canvas(
        app=celery_app,
        run_id=run_id,
        run_company_ids=run_company_ids,
    ).apply_async()

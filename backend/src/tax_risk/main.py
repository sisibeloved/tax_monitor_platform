from collections.abc import Awaitable, Callable
import logging
import re
from fastapi import FastAPI, Request, Response
from threading import BoundedSemaphore
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from tax_risk.api.routes.health import router as health_router
from tax_risk.api.routes.health import DefaultReadinessProbe, ReadinessProbe
from tax_risk.api.routes.audit import router as audit_router
from tax_risk.api.routes.ingest import router as ingest_router
from tax_risk.api.routes.cases import router as cases_router
from tax_risk.api.routes.dashboard import router as dashboard_router
from tax_risk.api.routes.master_data import router as master_data_router
from tax_risk.api.routes.monthly_semantic import router as monthly_semantic_router
from tax_risk.api.routes.operations import router as operations_router
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
from tax_risk.application.exports import (
    ExportObjectStore,
    ExportService,
    FileExportObjectStore,
)
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.config import Settings
from tax_risk.observability.context import observability_context
from tax_risk.observability.metrics import DEFAULT_METRICS, MetricRegistry
from tax_risk.observability.tracing import configure_structured_logging, start_span
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
    export_dispatcher: Callable[[UUID], None] | None = None,
    export_object_store: ExportObjectStore | None = None,
    readiness_probe: ReadinessProbe | None = None,
    metrics_registry: MetricRegistry | None = None,
) -> FastAPI:
    """Create the tax risk monitoring API."""

    resolved_settings = settings or Settings()
    if resolved_settings.environment == "production":
        configure_structured_logging()
    app = FastAPI(title="Group Income Tax Risk Monitoring Platform")
    app.state.uow_factory = uow_factory or UnitOfWork
    app.state.metrics_registry = metrics_registry or DEFAULT_METRICS
    app.state.audit_service = AuditService(app.state.uow_factory)
    resolved_export_store = export_object_store or FileExportObjectStore(
        resolved_settings.export_storage_path
    )
    app.state.export_service = ExportService(
        app.state.uow_factory,
        BusinessEntertainmentReportingService(app.state.uow_factory),
        resolved_export_store,
        app.state.audit_service,
        resolved_settings,
    )
    app.state.export_dispatcher = export_dispatcher or _dispatch_export_job
    app.state.readiness_probe = readiness_probe or DefaultReadinessProbe(
        settings=resolved_settings,
        uow_factory=app.state.uow_factory,
        object_store=resolved_export_store,
    )
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
        with observability_context(request_id=request_id):
            with start_span(f"HTTP {request.method}", logger):
                try:
                    response = await call_next(request)
                except Exception:
                    _append_http_audit(request, 500, app.state.audit_service, request_id)
                    _record_http_metrics(request, 500, app.state.metrics_registry)
                    raise
                _append_http_audit(
                    request,
                    response.status_code,
                    app.state.audit_service,
                    request_id,
                )
                _record_http_metrics(
                    request,
                    response.status_code,
                    app.state.metrics_registry,
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
    app.include_router(operations_router)
    return app


def _record_http_metrics(
    request: Request,
    status_code: int,
    registry: MetricRegistry,
) -> None:
    route = request.scope.get("route")
    raw_path = getattr(route, "path", request.url.path)
    metric_path = re.sub(r"\{[^}]+\}", ":param", str(raw_path))
    registry.metric("tax_risk_http_request_total").inc(
        {
            "method": request.method,
            "path": metric_path,
            "status": str(status_code),
        }
    )
    if status_code in {401, 403}:
        action = f"HTTP_{request.method}_{metric_path}".replace("/", "_").replace(
            ":", "_"
        )[:128]
        registry.metric("tax_risk_authorization_failure_total").inc(
            {"action": action, "reason_code": f"HTTP_{status_code}"}
        )


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

    with observability_context(run_id=run_id):
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

    with observability_context(run_id=run_id):
        build_monthly_semantic_canvas(
            app=celery_app,
            run_id=run_id,
            run_company_ids=run_company_ids,
        ).apply_async()


def _dispatch_export_job(job_id: UUID) -> None:
    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.exports import RENDER_EXPORT_TASK

    celery_app.send_task(RENDER_EXPORT_TASK, args=(str(job_id),))

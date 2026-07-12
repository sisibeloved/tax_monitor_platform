from fastapi import FastAPI
from threading import BoundedSemaphore

from tax_risk.api.routes.health import router as health_router
from tax_risk.api.routes.ingest import router as ingest_router
from tax_risk.api.routes.master_data import router as master_data_router
from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits
from tax_risk.application.ingest import (
    AdapterFactory,
    UowFactory,
    create_csv_adapter,
)
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork


def create_app(
    *,
    uow_factory: UowFactory | None = None,
    adapter_factory: AdapterFactory | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create the tax risk monitoring API."""

    resolved_settings = settings or Settings()
    app = FastAPI(title="Group Income Tax Risk Monitoring Platform")
    app.state.uow_factory = uow_factory or UnitOfWork
    app.state.adapter_factory = adapter_factory or create_csv_adapter
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
    app.include_router(health_router)
    app.include_router(ingest_router)
    app.include_router(master_data_router)
    return app

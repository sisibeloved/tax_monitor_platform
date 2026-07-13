from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]

from tax_risk.application.exports import ExportService, build_default_export_service
from tax_risk.security.context import principal_context
from tax_risk.security.service_scope import service_principal, verify_service_scope_token


EXPORT_QUEUE = "exports"
RENDER_EXPORT_TASK = "tax_risk.exports.render"


def register_export_tasks(
    *,
    app: Celery,
    service_factory: Callable[[], ExportService],
) -> None:
    @app.task(  # type: ignore[untyped-decorator]
        name=RENDER_EXPORT_TASK,
        queue=EXPORT_QUEUE,
        shared=False,
    )
    def render_export(
        job_id: str,
        authorization_version: str,
        scope_token: str,
    ) -> dict[str, Any]:
        scope = verify_service_scope_token(
            scope_token,
            secret=str(app.conf.worker_scope_secret),
            expected_queue=EXPORT_QUEUE,
            expected_run_type="EXPORT",
            expected_batch_id=job_id,
        )
        with principal_context(service_principal(scope)):
            view = service_factory().render_export(
                UUID(job_id),
                authorization_version=authorization_version,
            )
        return {
            "job_id": str(view.id),
            "status": view.status,
            "row_count": view.row_count,
            "checksum": view.checksum,
        }


def default_export_service_factory() -> ExportService:
    return build_default_export_service()


__all__ = [
    "EXPORT_QUEUE",
    "RENDER_EXPORT_TASK",
    "default_export_service_factory",
    "register_export_tasks",
]

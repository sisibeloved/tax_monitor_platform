from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]

from tax_risk.application.exports import ExportService, build_default_export_service


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
    )
    def render_export(job_id: str) -> dict[str, Any]:
        view = service_factory().render_export(UUID(job_id))
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

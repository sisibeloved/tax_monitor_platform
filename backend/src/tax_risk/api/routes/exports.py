"""Asynchronous, scope-frozen export job endpoints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from tax_risk.api.dependencies import require_exporter
from tax_risk.application.exports import ExportJobView, ExportNotFound, ExportNotReady, ExportService
from tax_risk.domain.exports import ExportType
from tax_risk.security.policies import ResourceNotFound
from tax_risk.security.principal import Principal


class ExportCreateRequest(BaseModel):
    export_type: ExportType
    filters: dict[str, Any]


class ExportJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    export_type: str
    requester_subject: str
    company_ids: tuple[str, ...]
    normalized_filters: dict[str, object]
    schema_version: str
    status: str
    row_count: int | None
    checksum: str | None
    object_key: str | None
    failure_code: str | None
    expires_at: datetime
    created_at: datetime
    completed_at: datetime | None


class ExportJobListResponse(BaseModel):
    items: tuple[ExportJobResponse, ...]


class DownloadUrlResponse(BaseModel):
    url: str


router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


def _service(request: Request) -> ExportService:
    return cast(ExportService, request.app.state.export_service)


@router.post("", response_model=ExportJobResponse, status_code=status.HTTP_202_ACCEPTED)
def create_export(
    body: ExportCreateRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_exporter)],
) -> ExportJobView:
    service = _service(request)
    try:
        job = service.create_export(
            principal,
            export_type=body.export_type,
            filters=body.filters,
        )
    except (ResourceNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    dispatcher = cast(Callable[[UUID], None], request.app.state.export_dispatcher)
    dispatcher(job.id)
    request.state.audit_company_ids = frozenset(UUID(value) for value in job.company_ids)
    request.state.audit_row_count = 1
    return job


@router.get("", response_model=ExportJobListResponse)
def list_exports(
    request: Request,
    principal: Annotated[Principal, Depends(require_exporter)],
) -> ExportJobListResponse:
    rows = _service(request).list_exports(principal)
    request.state.audit_row_count = len(rows)
    return ExportJobListResponse(
        items=tuple(ExportJobResponse.model_validate(row) for row in rows)
    )


@router.get("/{job_id}", response_model=ExportJobResponse)
def get_export(
    job_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_exporter)],
) -> ExportJobView:
    try:
        job = _service(request).get_export(principal, job_id)
    except ExportNotFound as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    request.state.audit_company_ids = frozenset(UUID(value) for value in job.company_ids)
    request.state.audit_row_count = 1
    return job


@router.post("/{job_id}/download-url", response_model=DownloadUrlResponse)
def create_download_url(
    job_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_exporter)],
) -> DownloadUrlResponse:
    try:
        url = _service(request).issue_download_url(principal, job_id)
    except ExportNotFound as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    except ExportNotReady as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "EXPORT_NOT_READY", "message": "导出文件尚未就绪"},
        ) from exc
    return DownloadUrlResponse(url=url)


@router.get("/{job_id}/content")
def download_export(
    job_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_exporter)],
    expires: Annotated[int, Query(gt=0)],
    signature: Annotated[str, Query(min_length=64, max_length=64)],
) -> Response:
    try:
        payload = _service(request).download(
            principal,
            job_id,
            expires=expires,
            signature=signature,
        )
    except (ExportNotFound, ExportNotReady) as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    request.state.audit_row_count = 1
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="tax-risk-export.xlsx"',
            "Cache-Control": "private, no-store",
        },
    )


__all__ = ["router"]

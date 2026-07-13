"""Read-only, company-scoped audit ledger API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from tax_risk.api.dependencies import require_audit_reader
from tax_risk.application.audit import AuditService
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entity_type: str
    entity_id: UUID
    action: str
    actor: str
    actor_roles: list[str]
    company_ids: list[str]
    request_id: str | None
    filters_hash: str | None
    row_count: int | None
    before_summary: dict[str, Any]
    after_summary: dict[str, Any]
    result: str
    reason_code: str | None
    occurred_at: datetime


class AuditEventListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: tuple[AuditEventResponse, ...]


router = APIRouter(prefix="/api/v1/audit-events", tags=["audit"])


@router.get("", response_model=AuditEventListResponse)
def list_audit_events(
    request: Request,
    principal: Annotated[Principal, Depends(require_audit_reader)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditEventListResponse:
    service = AuditService(
        cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    )
    total, rows = service.search(principal, page=page, page_size=page_size)
    return AuditEventListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=tuple(AuditEventResponse.model_validate(row) for row in rows),
    )


__all__ = ["router"]

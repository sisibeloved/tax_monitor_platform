"""业务招待费风险复核、SAP覆盖和精确关联解决接口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from tax_risk.api.dependencies import company_scope, require_case_writer, require_reader
from tax_risk.api.schemas import (
    ResolveBusinessEntertainmentCaseRequest,
    ResolveBusinessEntertainmentCaseResponse,
    SapLinkCoverageItemResponse,
    SapLinkCoverageListResponse,
)
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.application.case_merge import (
    CaseMergeConflictError,
    CaseMergeNotFoundError,
    CaseMergeService,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


router = APIRouter(
    prefix="/api/v1/business-entertainment",
    tags=["business-entertainment"],
)


@router.get("/sap-link-coverage", response_model=SapLinkCoverageListResponse)
def list_sap_link_coverage(
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
    fiscal_year: Annotated[int | None, Query(ge=2000, le=9999)] = None,
    period: Annotated[int | None, Query(ge=1, le=12)] = None,
    company: UUID | None = None,
) -> SapLinkCoverageListResponse:
    scope = company_scope(principal, requested_company_id=company)
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    rows = BusinessEntertainmentReportingService(uow_factory).list_sap_link_coverage(
        company_scope=scope,
        fiscal_year=fiscal_year,
        period=period,
        company_id=company,
    )
    return SapLinkCoverageListResponse(
        total=len(rows),
        items=tuple(SapLinkCoverageItemResponse.model_validate(row) for row in rows),
    )


@router.post(
    "/risk-cases/{case_id}/resolve-to-sap",
    response_model=ResolveBusinessEntertainmentCaseResponse,
)
def resolve_case_to_sap(
    case_id: UUID,
    body: ResolveBusinessEntertainmentCaseRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_case_writer)],
) -> ResolveBusinessEntertainmentCaseResponse:
    scope = company_scope(principal)
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    try:
        BusinessEntertainmentReportingService(uow_factory).get_case(
            case_id,
            company_scope=scope,
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found") from error
    try:
        result = CaseMergeService(uow_factory).resolve_to_sap(
            business_case_id=case_id,
            evidence_link_id=body.evidence_link_id,
            expected_row_version=body.expected_row_version,
            actor=principal.subject,
        )
    except CaseMergeNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found") from error
    except CaseMergeConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.error_code, "message": str(error)},
        ) from error
    return ResolveBusinessEntertainmentCaseResponse.model_validate(result)


__all__ = ["router"]

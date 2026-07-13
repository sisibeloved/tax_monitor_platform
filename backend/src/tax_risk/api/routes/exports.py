"""Scoped business-entertainment root-case export."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from tax_risk.api.dependencies import company_scope, require_reader
from tax_risk.application.business_entertainment.export import (
    build_export_rows,
    render_xlsx,
)
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


def get_reporting_service(request: Request) -> BusinessEntertainmentReportingService:
    return BusinessEntertainmentReportingService(
        cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    )


@router.get("/business-entertainment.xlsx")
def export_business_entertainment(
    principal: Annotated[Principal, Depends(require_reader)],
    reporting: BusinessEntertainmentReportingService = Depends(get_reporting_service),
) -> StreamingResponse:
    scope = company_scope(principal)
    payload = render_xlsx(
        build_export_rows(reporting.list_root_cases(company_scope=scope))
    )
    return StreamingResponse(
        BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                'attachment; filename="business-entertainment-risks.xlsx"'
            )
        },
    )


__all__ = ["router"]

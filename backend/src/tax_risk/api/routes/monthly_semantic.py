"""Trigger and inspect frozen monthly semantic monitoring runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Never, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from tax_risk.api.dependencies import company_scope, require_case_writer, require_reader
from tax_risk.api.schemas import MonthlyRunRequest, MonthlyRunResponse
from tax_risk.application.monthly_semantic_runs import (
    MonthlyRunPlan,
    MonthlySemanticRunError,
    MonthlySemanticRunService,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


class MonthlyDispatcher(Protocol):
    def __call__(
        self,
        *,
        run_id: UUID,
        run_company_ids: tuple[UUID, ...],
    ) -> None: ...


router = APIRouter(prefix="/api/v1/monthly-semantic", tags=["monthly-semantic"])


def _service(request: Request) -> MonthlySemanticRunService:
    factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    return MonthlySemanticRunService(factory)


@router.post("/runs", response_model=MonthlyRunResponse, status_code=status.HTTP_202_ACCEPTED)
def create_run(
    body: MonthlyRunRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_case_writer)],
) -> MonthlyRunResponse:
    service = _service(request)
    try:
        plan: MonthlyRunPlan = service.start_run(
            monitoring_type=body.monitoring_type,
            period=body.period,
            company_codes=body.company_codes,
            snapshot_set_id=body.snapshot_set_id,
            semantic_version_set_id=body.semantic_version_set_id,
            allowed_company_ids=company_scope(principal),
        )
    except MonthlySemanticRunError as error:
        _raise_monthly_error(error)
    if plan.run_company_ids:
        dispatcher = cast(MonthlyDispatcher, request.app.state.monthly_semantic_dispatcher)
        try:
            dispatcher(
                run_id=plan.run.run_id,
                run_company_ids=plan.run_company_ids,
            )
        except Exception as error:
            service.mark_dispatch_failed(plan.run.run_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "BROKER_DISPATCH_FAILED"},
            ) from error
    return MonthlyRunResponse.model_validate(plan.run)


@router.get("/runs/{run_id}", response_model=MonthlyRunResponse)
def get_run(
    run_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
) -> MonthlyRunResponse:
    try:
        view = _service(request).get_status(
            run_id,
            allowed_company_ids=company_scope(principal),
        )
    except MonthlySemanticRunError as error:
        _raise_monthly_error(error)
    return MonthlyRunResponse.model_validate(view)


def _raise_monthly_error(error: MonthlySemanticRunError) -> Never:
    if error.error_code in {"MONTHLY_RUN_NOT_FOUND", "SNAPSHOT_SET_NOT_FOUND"}:
        code = status.HTTP_404_NOT_FOUND
    elif error.error_code in {
        "SNAPSHOT_SET_NOT_PUBLISHED",
        "SEMANTIC_VERSION_SET_NOT_PUBLISHED",
        "SEMANTIC_VERSION_MEMBER_NOT_PUBLISHED",
    }:
        code = status.HTTP_409_CONFLICT
    elif error.error_code == "MONTHLY_COMPANY_OUT_OF_SCOPE":
        code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(
        status_code=code,
        detail={"code": error.error_code, "message": str(error)},
    )


__all__ = ["MonthlyDispatcher", "router"]

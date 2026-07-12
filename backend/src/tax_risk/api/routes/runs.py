"""Quarterly monitoring-run command and status endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Never, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from tax_risk.api.dependencies import require_group_tax, require_reader
from tax_risk.api.schemas import (
    QuarterlyRunCreateRequest,
    QuarterlyRunResponse,
    QuarterlyRunStartResponse,
)
from tax_risk.application.quarterly_batches import (
    QuarterlyBatchError,
    QuarterlyBatchPlan,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import MonitoringRunStatus
from tax_risk.security.principal import Principal


class QuarterlyBatchStarter(Protocol):
    def start_batch(
        self,
        *,
        fiscal_year: int,
        quarter: int,
        snapshot_set_id: UUID,
        rule_version_id: UUID,
    ) -> QuarterlyBatchPlan: ...


QuarterlyBatchStarterFactory = Callable[[], QuarterlyBatchStarter]


class QuarterlyDispatcher(Protocol):
    def __call__(
        self,
        *,
        run_id: UUID,
        run_company_ids: tuple[UUID, ...],
    ) -> None: ...


router = APIRouter(prefix="/api/v1/quarterly-runs", tags=["quarterly-runs"])


@router.post(
    "",
    response_model=QuarterlyRunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_quarterly_run(
    body: QuarterlyRunCreateRequest,
    request: Request,
    _principal: Annotated[Principal, Depends(require_group_tax)],
) -> QuarterlyRunStartResponse:
    service_factory = cast(
        QuarterlyBatchStarterFactory,
        request.app.state.quarterly_batch_service_factory,
    )
    dispatcher = cast(QuarterlyDispatcher, request.app.state.quarterly_dispatcher)
    try:
        plan = service_factory().start_batch(
            fiscal_year=body.fiscal_year,
            quarter=body.quarter,
            snapshot_set_id=body.snapshot_set_id,
            rule_version_id=body.rule_version,
        )
    except QuarterlyBatchError as error:
        _raise_batch_error(error)
    if plan.run_company_ids:
        dispatcher(run_id=plan.run_id, run_company_ids=plan.run_company_ids)
    return QuarterlyRunStartResponse(
        run_id=plan.run_id,
        run_key=plan.run_key,
        status=MonitoringRunStatus.RUNNING,
        dispatched_company_count=len(plan.run_company_ids),
    )


@router.get("/{run_id}", response_model=QuarterlyRunResponse)
def get_quarterly_run(
    run_id: UUID,
    request: Request,
    _principal: Annotated[Principal, Depends(require_reader)],
) -> object:
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        run = uow.risks.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        return QuarterlyRunResponse.model_validate(run)


def _raise_batch_error(error: QuarterlyBatchError) -> Never:
    if error.error_code in {"SNAPSHOT_SET_NOT_FOUND", "RULE_VERSION_NOT_FOUND"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif error.error_code in {
        "SNAPSHOT_SET_NOT_PUBLISHED",
        "RULE_VERSION_NOT_PUBLISHED",
        "RULE_MANIFEST_NOT_APPROVED",
        "SNAPSHOT_SET_MEMBER_COUNT_MISMATCH",
    }:
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.error_code, "message": str(error)},
    )


__all__ = ["QuarterlyBatchStarterFactory", "QuarterlyDispatcher", "router"]

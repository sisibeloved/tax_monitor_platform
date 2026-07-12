"""Quarterly monitoring-run command and status endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Never, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select

from tax_risk.api.dependencies import company_scope, require_group_tax, require_reader
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
from tax_risk.persistence.risk_models import (
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    MonitoringRunStatus,
)
from tax_risk.persistence.snapshot_models import SnapshotSetMember
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
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        persisted_run = uow.risks.get_run(plan.run_id)
        if persisted_run is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "MONITORING_RUN_NOT_PERSISTED"},
            )
        persisted_status = persisted_run.status
    return QuarterlyRunStartResponse(
        run_id=plan.run_id,
        run_key=plan.run_key,
        status=persisted_status,
        dispatched_company_count=len(plan.run_company_ids),
    )


@router.get("/{run_id}", response_model=QuarterlyRunResponse)
def get_quarterly_run(
    run_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
) -> QuarterlyRunResponse:
    scope = company_scope(principal)
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        if scope is not None:
            return _get_scoped_run_response(uow, run_id=run_id, scope=scope)
        run = uow.risks.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        return QuarterlyRunResponse.model_validate(run)


def _get_scoped_run_response(
    uow: UnitOfWork,
    *,
    run_id: UUID,
    scope: frozenset[UUID],
) -> QuarterlyRunResponse:
    row = uow.session.execute(
        select(
            MonitoringRun,
            func.count(MonitoringRunCompany.id).label("requested"),
            func.count(MonitoringRunCompany.id)
            .filter(
                MonitoringRunCompany.status == MonitoringRunCompanyStatus.SUCCEEDED
            )
            .label("succeeded"),
            func.count(MonitoringRunCompany.id)
            .filter(MonitoringRunCompany.status == MonitoringRunCompanyStatus.BLOCKED)
            .label("blocked"),
            func.count(MonitoringRunCompany.id)
            .filter(MonitoringRunCompany.status == MonitoringRunCompanyStatus.FAILED)
            .label("failed"),
            func.count(MonitoringRunCompany.id)
            .filter(
                MonitoringRunCompany.status.in_(
                    (
                        MonitoringRunCompanyStatus.PENDING,
                        MonitoringRunCompanyStatus.RUNNING,
                        MonitoringRunCompanyStatus.RETRY_PENDING,
                    )
                )
            )
            .label("active"),
            func.max(MonitoringRunCompany.finished_at).label("finished_at"),
        )
        .join(MonitoringRunCompany, MonitoringRunCompany.run_id == MonitoringRun.id)
        .join(
            SnapshotSetMember,
            SnapshotSetMember.id == MonitoringRunCompany.snapshot_set_member_id,
        )
        .where(
            MonitoringRun.id == run_id,
            SnapshotSetMember.company_id.in_(scope),
        )
        .group_by(MonitoringRun.id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    run, requested, succeeded, blocked, failed, active, finished_at = row
    scoped_status = _derive_run_status(
        requested=requested,
        succeeded=succeeded,
        active=active,
    )
    return QuarterlyRunResponse(
        id=run.id,
        run_key=run.run_key,
        status=scoped_status,
        fiscal_year=run.fiscal_year,
        quarter=run.quarter,
        snapshot_set_id=run.snapshot_set_id,
        rule_version_id=run.rule_version_id,
        requested_company_count=requested,
        succeeded_company_count=succeeded,
        blocked_company_count=blocked,
        failed_company_count=failed,
        started_at=run.started_at,
        finished_at=None if scoped_status == MonitoringRunStatus.RUNNING else finished_at,
        failure_reason=(
            "NO_COMPANY_SUCCEEDED"
            if scoped_status == MonitoringRunStatus.FAILED
            else None
        ),
    )


def _derive_run_status(
    *,
    requested: int,
    succeeded: int,
    active: int,
) -> MonitoringRunStatus:
    if active:
        return MonitoringRunStatus.RUNNING
    if succeeded == requested:
        return MonitoringRunStatus.SUCCEEDED
    if succeeded:
        return MonitoringRunStatus.PARTIAL_SUCCESS
    return MonitoringRunStatus.FAILED


def _raise_batch_error(error: QuarterlyBatchError) -> Never:
    if error.error_code in {"SNAPSHOT_SET_NOT_FOUND", "RULE_VERSION_NOT_FOUND"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif error.error_code in {
        "SNAPSHOT_SET_NOT_PUBLISHED",
        "RULE_VERSION_NOT_PUBLISHED",
        "RULE_MANIFEST_NOT_APPROVED",
        "QUARTERLY_RULE_MANIFEST_INVALID",
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

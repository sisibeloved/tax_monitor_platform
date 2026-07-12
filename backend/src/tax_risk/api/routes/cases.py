"""Risk-case search and review workflow endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status as http_status
from sqlalchemy import func, select

from tax_risk.api.dependencies import (
    actor_role,
    company_scope,
    require_case_writer,
    require_reader,
)
from tax_risk.api.schemas import (
    RiskCaseAction,
    RiskCaseActionRequest,
    RiskCaseActionResponse,
    RiskCaseItemResponse,
    RiskCaseListResponse,
)
from tax_risk.domain.cases import CaseStatus, InvalidCaseTransition, transition_case
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    DetectionRecord,
    MonitorType,
    MonitoringRun,
    ReviewAction,
    RiskCase,
    RiskCaseStatus,
)
from tax_risk.security.principal import Principal
from tax_risk.security.principal import GROUP_TAX_ROLE


router = APIRouter(prefix="/api/v1/risk-cases", tags=["risk-cases"])
_ACTION_TRANSITIONS: dict[
    RiskCaseAction,
    frozenset[tuple[CaseStatus, CaseStatus]],
] = {
    RiskCaseAction.ASSIGN: frozenset({(CaseStatus.NEW, CaseStatus.ASSIGNED)}),
    RiskCaseAction.REQUEST_COMPANY_CONFIRMATION: frozenset(
        {(CaseStatus.ASSIGNED, CaseStatus.PENDING_COMPANY_CONFIRMATION)}
    ),
    RiskCaseAction.REQUEST_ADJUSTMENT: frozenset(
        {(CaseStatus.PENDING_COMPANY_CONFIRMATION, CaseStatus.PENDING_ADJUSTMENT)}
    ),
    RiskCaseAction.SUBMIT_ADJUSTMENT: frozenset(
        {(CaseStatus.PENDING_ADJUSTMENT, CaseStatus.ADJUSTED_PENDING_REVIEW)}
    ),
    RiskCaseAction.SUBMIT_GROUP_REVIEW: frozenset(
        {(CaseStatus.PENDING_COMPANY_CONFIRMATION, CaseStatus.GROUP_REVIEW)}
    ),
    RiskCaseAction.REQUEST_EVIDENCE: frozenset(
        {(CaseStatus.PENDING_COMPANY_CONFIRMATION, CaseStatus.EVIDENCE_REQUIRED)}
    ),
    RiskCaseAction.RESUBMIT_CONFIRMATION: frozenset(
        {(CaseStatus.EVIDENCE_REQUIRED, CaseStatus.PENDING_COMPANY_CONFIRMATION)}
    ),
    RiskCaseAction.CLOSE: frozenset(
        {
            (CaseStatus.ADJUSTED_PENDING_REVIEW, CaseStatus.CLOSED),
            (CaseStatus.GROUP_REVIEW, CaseStatus.CLOSED),
        }
    ),
}


@router.get("", response_model=RiskCaseListResponse)
def list_risk_cases(
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
    fiscal_year: Annotated[int, Query(ge=2000, le=9999)],
    quarter: Annotated[int, Query(ge=1, le=4)],
    monitoring_type: MonitorType | None = None,
    direction: str | None = None,
    case_status: Annotated[RiskCaseStatus | None, Query(alias="status")] = None,
    company: UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RiskCaseListResponse:
    scope = company_scope(principal, requested_company_id=company)
    conditions = [
        MonitoringRun.fiscal_year == fiscal_year,
        MonitoringRun.quarter == quarter,
    ]
    if monitoring_type is not None:
        conditions.append(RiskCase.monitor_type == monitoring_type)
    if direction is not None:
        conditions.append(RiskCase.risk_direction == direction)
    if case_status is not None:
        conditions.append(RiskCase.status == case_status)
    if company is not None:
        conditions.append(RiskCase.company_id == company)
    if scope is not None:
        conditions.append(RiskCase.company_id.in_(scope))

    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        joined = (
            RiskCase.__table__.join(
                DetectionRecord.__table__,
                RiskCase.latest_detection_id == DetectionRecord.id,
            )
            .join(MonitoringRun.__table__, DetectionRecord.run_id == MonitoringRun.id)
            .join(Company.__table__, RiskCase.company_id == Company.id)
        )
        total = uow.session.scalar(
            select(func.count(RiskCase.id)).select_from(joined).where(*conditions)
        )
        rows = uow.session.execute(
            select(
                RiskCase,
                DetectionRecord,
                Company.company_code,
                Company.company_name,
            )
            .select_from(joined)
            .where(*conditions)
            .order_by(Company.company_code, RiskCase.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = tuple(
            RiskCaseItemResponse(
                id=risk_case.id,
                company_id=risk_case.company_id,
                company_code=company_code,
                company_name=company_name,
                latest_detection_id=risk_case.latest_detection_id,
                run_id=detection.run_id,
                monitoring_type=risk_case.monitor_type,
                calculation_status=detection.calculation_status,
                input_amount=detection.input_amount,
                result_amount=detection.result_amount,
                difference_amount=detection.difference_amount,
                tax_burden_rate=detection.tax_burden_rate,
                tax_burden_deviation=detection.tax_burden_deviation,
                not_calculated_reason=detection.not_calculated_reason,
                alert_code=detection.alert_code,
                risk_direction=risk_case.risk_direction,
                risk_amount=risk_case.risk_amount,
                risk_rate=risk_case.risk_rate,
                currency=risk_case.currency,
                amount_scale=risk_case.amount_scale,
                status=risk_case.status,
                priority=risk_case.priority,
                assignee=risk_case.assignee,
                row_version=risk_case.row_version,
            )
            for risk_case, detection, company_code, company_name in rows
        )
    return RiskCaseListResponse(
        total=total or 0,
        page=page,
        page_size=page_size,
        items=items,
    )


@router.post("/{case_id}/actions", response_model=RiskCaseActionResponse)
def apply_case_action(
    case_id: UUID,
    body: RiskCaseActionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_case_writer)],
) -> RiskCaseActionResponse:
    scope = company_scope(principal)
    conditions = [RiskCase.id == case_id]
    if scope is not None:
        conditions.append(RiskCase.company_id.in_(scope))

    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        risk_case = uow.session.scalar(
            select(RiskCase).where(*conditions).with_for_update()
        )
        if risk_case is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Not Found",
            )
        from_status = CaseStatus(risk_case.status.value)
        to_status = CaseStatus(body.to_status.value)
        if body.to_status == RiskCaseStatus.CLOSED and not principal.has_role(
            GROUP_TAX_ROLE
        ):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        if (from_status, to_status) not in _ACTION_TRANSITIONS[body.action]:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail={
                    "code": "ACTION_TRANSITION_MISMATCH",
                    "message": (
                        f"action {body.action.value} does not match transition "
                        f"from {from_status.value} to {to_status.value}"
                    ),
                },
            )
        try:
            target = transition_case(from_status, to_status)
        except InvalidCaseTransition as error:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail={"code": "INVALID_CASE_TRANSITION", "message": str(error)},
            ) from error

        risk_case.status = RiskCaseStatus(target.value)
        if body.assignee is not None:
            risk_case.assignee = body.assignee
        risk_case.row_version += 1
        uow.risks.add_review_action(
            ReviewAction(
                risk_case_id=risk_case.id,
                actor=principal.subject,
                actor_role=actor_role(principal),
                from_status=RiskCaseStatus(from_status.value),
                action=body.action.value,
                to_status=risk_case.status,
                reason=body.reason,
                attachment_refs=list(body.attachment_refs),
                correction_voucher_no=body.correction_voucher_no,
            )
        )
        uow.session.flush()
        response = RiskCaseActionResponse.model_validate(risk_case)
        uow.commit()
        return response


__all__ = ["router"]

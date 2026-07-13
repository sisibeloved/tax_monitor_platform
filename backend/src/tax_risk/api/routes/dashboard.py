"""Scoped quarterly dashboard and detection-detail read endpoints."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from tax_risk.api.dependencies import company_scope, require_reader
from tax_risk.api.schemas import (
    DashboardCompanyPageResponse,
    DashboardCompanyResponse,
    DetectionDetailResponse,
    QuarterlyDashboardResponse,
)
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    DetectionRecord,
    MonitorType,
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    RiskCase,
)
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSetMember,
    SnapshotStatus,
)
from tax_risk.security.principal import Principal


router = APIRouter(tags=["dashboard"])
_ZERO_AMOUNT = Decimal("0.000000000000")
_QUARTERLY_MONITOR_TYPES = (
    MonitorType.ACCRUAL_ACCURACY,
    MonitorType.TAX_BURDEN,
    MonitorType.POTENTIAL_TAX_COST,
)


@router.get("/api/v1/dashboard/quarterly", response_model=QuarterlyDashboardResponse)
def get_quarterly_dashboard(
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
    fiscal_year: Annotated[int, Query(ge=2000, le=9999)],
    quarter: Annotated[int, Query(ge=1, le=4)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> QuarterlyDashboardResponse:
    scope = company_scope(principal)
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        run = uow.session.scalar(
            select(MonitoringRun)
            .where(
                MonitoringRun.fiscal_year == fiscal_year,
                MonitoringRun.quarter == quarter,
            )
            .order_by(MonitoringRun.created_at.desc(), MonitoringRun.id.desc())
            .limit(1)
        )
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

        company_conditions = [MonitoringRunCompany.run_id == run.id]
        risk_conditions = [DetectionRecord.run_id == run.id]
        potential_cost_conditions = [
            DetectionRecord.run_id == run.id,
            DetectionRecord.monitor_type == MonitorType.POTENTIAL_TAX_COST,
        ]
        if scope is not None:
            company_conditions.append(SnapshotSetMember.company_id.in_(scope))
            risk_conditions.append(RiskCase.company_id.in_(scope))
            potential_cost_conditions.append(DetectionRecord.company_id.in_(scope))

        company_join = (
            MonitoringRunCompany.__table__.join(
                SnapshotSetMember.__table__,
                MonitoringRunCompany.snapshot_set_member_id == SnapshotSetMember.id,
            )
            .join(
                AccountingSnapshot.__table__,
                SnapshotSetMember.snapshot_id == AccountingSnapshot.id,
            )
            .join(Company.__table__, SnapshotSetMember.company_id == Company.id)
        )
        coverage_company_count = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id))
            .select_from(company_join)
            .where(*company_conditions)
        ) or 0
        data_ready_count = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id))
            .select_from(company_join)
            .where(
                *company_conditions,
                AccountingSnapshot.status == SnapshotStatus.PUBLISHED,
            )
        ) or 0
        blocked_count = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id))
            .select_from(company_join)
            .where(
                *company_conditions,
                MonitoringRunCompany.status == MonitoringRunCompanyStatus.BLOCKED,
            )
        ) or 0

        risk_join = RiskCase.__table__.join(
            DetectionRecord.__table__,
            RiskCase.latest_detection_id == DetectionRecord.id,
        )
        risk_company_count = uow.session.scalar(
            select(func.count(func.distinct(RiskCase.company_id)))
            .select_from(risk_join)
            .where(*risk_conditions)
        ) or 0
        potential_tax_cost_total = uow.session.scalar(
            select(func.sum(DetectionRecord.difference_amount)).where(
                *potential_cost_conditions
            )
        ) or _ZERO_AMOUNT
        type_rows = uow.session.execute(
            select(RiskCase.monitor_type, func.count(RiskCase.id))
            .select_from(risk_join)
            .where(*risk_conditions)
            .group_by(RiskCase.monitor_type)
        ).all()
        monitoring_type_counts = {
            monitor_type: 0 for monitor_type in _QUARTERLY_MONITOR_TYPES
        }
        for monitor_type, count in type_rows:
            monitoring_type_counts[monitor_type] = count

        money_context = uow.session.execute(
            select(AccountingSnapshot.currency, AccountingSnapshot.amount_scale)
            .select_from(company_join)
            .where(*company_conditions)
            .order_by(Company.company_code)
            .limit(1)
        ).one_or_none()
        currency, amount_scale = money_context or ("CNY", 2)

        risk_counts = (
            select(
                RiskCase.company_id.label("company_id"),
                func.count(RiskCase.id).label("risk_count"),
            )
            .select_from(risk_join)
            .where(*risk_conditions)
            .group_by(RiskCase.company_id)
            .subquery()
        )
        company_rows = uow.session.execute(
            select(
                SnapshotSetMember.company_id,
                Company.company_code,
                Company.company_name,
                AccountingSnapshot.status,
                MonitoringRunCompany.status,
                MonitoringRunCompany.error_message,
                func.coalesce(risk_counts.c.risk_count, 0),
            )
            .select_from(company_join)
            .outerjoin(
                risk_counts,
                risk_counts.c.company_id == SnapshotSetMember.company_id,
            )
            .where(*company_conditions)
            .order_by(Company.company_code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        companies = DashboardCompanyPageResponse(
            total=coverage_company_count,
            page=page,
            page_size=page_size,
            items=tuple(
                DashboardCompanyResponse(
                    company_id=company_id,
                    company_code=company_code,
                    company_name=company_name,
                    data_ready=snapshot_status == SnapshotStatus.PUBLISHED,
                    execution_status=execution_status,
                    blocked_reason=(
                        error_message
                        if execution_status == MonitoringRunCompanyStatus.BLOCKED
                        else None
                    ),
                    risk_count=risk_count,
                )
                for (
                    company_id,
                    company_code,
                    company_name,
                    snapshot_status,
                    execution_status,
                    error_message,
                    risk_count,
                ) in company_rows
            ),
        )
        return QuarterlyDashboardResponse(
            fiscal_year=fiscal_year,
            quarter=quarter,
            run_id=run.id,
            coverage_company_count=coverage_company_count,
            data_ready_count=data_ready_count,
            blocked_count=blocked_count,
            risk_company_count=risk_company_count,
            potential_tax_cost_total=potential_tax_cost_total,
            currency=currency,
            amount_scale=amount_scale,
            monitoring_type_counts=monitoring_type_counts,
            companies=companies,
        )


@router.get("/api/v1/detections/{detection_id}", response_model=DetectionDetailResponse)
def get_detection_detail(
    detection_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
) -> DetectionDetailResponse:
    scope = company_scope(principal)
    conditions = [DetectionRecord.id == detection_id]
    if scope is not None:
        conditions.append(DetectionRecord.company_id.in_(scope))

    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        detection = uow.session.scalar(select(DetectionRecord).where(*conditions))
        if detection is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        return DetectionDetailResponse(
            id=detection.id,
            run_id=detection.run_id,
            company_id=detection.company_id,
            snapshot_id=detection.snapshot_id,
            rule_version_id=detection.rule_version_id,
            tax_master_version_id=detection.tax_master_version_id,
            monitoring_type=detection.monitor_type,
            calculation_status=detection.calculation_status,
            input_amount=detection.input_amount,
            result_amount=detection.result_amount,
            difference_amount=detection.difference_amount,
            rate_value=detection.rate_value,
            tax_burden_rate=detection.tax_burden_rate,
            tax_burden_deviation=detection.tax_burden_deviation,
            currency=detection.currency,
            amount_scale=detection.amount_scale,
            formula_substitution=detection.formula_substitution,
            lineage=detection.lineage,
            structured_output=detection.structured_output,
            not_calculated_reason=detection.not_calculated_reason,
            alert_code=detection.alert_code,
            direction=detection.direction,
        )


__all__ = ["router"]

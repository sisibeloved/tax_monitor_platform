"""Privacy-safe operational summary and governed failed-company retry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import case, func, select

from tax_risk.api.dependencies import require_monitor_runner
from tax_risk.application.monthly_semantic_runs import MonthlySemanticRunService
from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.db import apply_principal_context
from tax_risk.observability.metrics import MetricRegistry
from tax_risk.persistence.business_entertainment_models import SapLinkCoverage
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    MonitoringRunType,
    RiskCase,
    RiskCaseStatus,
)
from tax_risk.persistence.snapshot_models import SnapshotSet, SnapshotSetStatus
from tax_risk.security.principal import Principal


router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


@router.get("/summary")
def get_operations_summary(
    request: Request,
    principal: Annotated[Principal, Depends(require_monitor_runner)],
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        apply_principal_context(uow.session, principal)
        runs = list(
            uow.session.scalars(
                select(MonitoringRun)
                .order_by(MonitoringRun.created_at.desc(), MonitoringRun.id.desc())
                .limit(20)
            )
        )
        run_ids = tuple(run.id for run in runs)
        counts_by_run: dict[UUID, dict[str, int]] = {
            run.id: {"succeeded": 0, "blocked": 0, "failed": 0} for run in runs
        }
        first_started: dict[UUID, datetime] = {}
        if run_ids:
            company_rows = uow.session.execute(
                select(
                    MonitoringRunCompany.run_id,
                    MonitoringRunCompany.status,
                    func.count(MonitoringRunCompany.id),
                    func.min(MonitoringRunCompany.started_at),
                )
                .where(MonitoringRunCompany.run_id.in_(run_ids))
                .group_by(MonitoringRunCompany.run_id, MonitoringRunCompany.status)
            ).all()
            for run_id, company_status, count_value, earliest_started in company_rows:
                key = _company_counter_key(company_status)
                if key is not None:
                    counts_by_run[run_id][key] += int(count_value)
                if earliest_started is not None:
                    current = first_started.get(run_id)
                    if current is None or earliest_started < current:
                        first_started[run_id] = earliest_started

        data_errors = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id)).where(
                MonitoringRunCompany.status.in_(
                    (
                        MonitoringRunCompanyStatus.BLOCKED,
                        MonitoringRunCompanyStatus.NOT_RUN,
                    )
                )
            )
        ) or 0
        technical_failures = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id)).where(
                MonitoringRunCompany.status == MonitoringRunCompanyStatus.FAILED
            )
        ) or 0
        provider_failures = uow.session.scalar(
            select(func.count(MonitoringRunCompany.id)).where(
                MonitoringRunCompany.status == MonitoringRunCompanyStatus.FAILED,
                MonitoringRunCompany.error_code.in_(
                    (
                        "PROVIDER_TIMEOUT",
                        "MODEL_PROVIDER_FAILED",
                        "MODEL_OUTPUT_INVALID",
                        "MONTHLY_COMPANY_EXECUTION_FAILED",
                    )
                ),
            )
        ) or 0
        tax_risks = uow.session.scalar(
            select(func.count(RiskCase.id)).where(RiskCase.status != RiskCaseStatus.CLOSED)
        ) or 0
        evidence_backlog = uow.session.scalar(
            select(func.count(RiskCase.id)).where(
                RiskCase.status == RiskCaseStatus.EVIDENCE_REQUIRED
            )
        ) or 0
        coverage = uow.session.execute(
            select(
                func.count(SapLinkCoverage.id),
                func.sum(
                    case((SapLinkCoverage.exact_evidence_link_id.is_not(None), 1), else_=0)
                ),
            )
        ).one()
        coverage_total = int(coverage[0] or 0)
        coverage_ratio = (
            float(coverage[1] or 0) / coverage_total if coverage_total else None
        )
        latest_snapshot = uow.session.scalar(
            select(SnapshotSet)
            .where(SnapshotSet.status == SnapshotSetStatus.PUBLISHED)
            .order_by(SnapshotSet.published_at.desc())
            .limit(1)
        )

    deadline = (
        latest_snapshot.published_at + timedelta(hours=48)
        if latest_snapshot is not None and latest_snapshot.published_at is not None
        else None
    )
    latest_output_ready = bool(runs and runs[0].output_ready_at is not None)
    delivery_status = _delivery_status(now, deadline, latest_output_ready)
    registry = cast(MetricRegistry, request.app.state.metrics_registry)
    if coverage_ratio is not None:
        registry.metric("tax_risk_link_coverage_ratio").set(
            {"source_pair": "BUSINESS_DOCUMENT_SAP"},
            coverage_ratio,
        )
    registry.metric("tax_risk_evidence_backlog").set(
        {"monitor_type": "ALL"},
        float(evidence_backlog),
    )
    if latest_snapshot is not None and latest_snapshot.published_at is not None:
        registry.metric("tax_risk_data_source_ready").set(
            {"source": "SNAPSHOT_SET"},
            1.0,
        )
        for run_type in ("QUARTERLY", "MONTHLY_SEMANTIC"):
            registry.metric("tax_risk_data_ready_timestamp_seconds").set(
                {"run_type": run_type},
                latest_snapshot.published_at.timestamp(),
            )
    for run in runs:
        if run.output_ready_at is not None:
            registry.metric("tax_risk_output_ready_timestamp_seconds").set(
                {"run_type": run.run_type.value, "scope": "BATCH"},
                run.output_ready_at.timestamp(),
            )
    return {
        "generated_at": now.isoformat(),
        "t_plus_2_deadline": deadline.isoformat() if deadline is not None else None,
        "delivery_status": delivery_status,
        "can_retry": True,
        "counters": {
            "data_errors": int(data_errors),
            "technical_failures": int(technical_failures),
            "tax_risks": int(tax_risks),
            "provider_failures": int(provider_failures),
            "evidence_backlog": int(evidence_backlog),
        },
        "link_coverage_ratio": coverage_ratio,
        "runs": [
            {
                "run_id": str(run.id),
                "run_type": run.run_type.value,
                "period": (
                    f"{run.fiscal_year}-Q{run.quarter}"
                    if run.run_type == MonitoringRunType.QUARTERLY
                    else cast(date, run.period).strftime("%Y-%m")
                ),
                "status": run.status.value,
                "queue_wait_seconds": _queue_wait_seconds(
                    run.started_at,
                    first_started.get(run.id),
                    now,
                ),
                "company_counts": counts_by_run[run.id],
            }
            for run in runs
        ],
    }


@router.post("/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_failed_companies(
    run_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_monitor_runner)],
) -> dict[str, object]:
    uow_factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    with uow_factory() as uow:
        apply_principal_context(uow.session, principal)
        run = uow.risks.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        run_type = run.run_type
    if run_type == MonitoringRunType.QUARTERLY:
        plan = QuarterlyBatchService(uow_factory).retry_failed(run_id=run_id)
        dispatcher = cast(Callable[..., None], request.app.state.quarterly_dispatcher)
        if plan.run_company_ids:
            dispatcher(run_id=run_id, run_company_ids=plan.run_company_ids)
        dispatched = len(plan.run_company_ids)
    else:
        company_ids = MonthlySemanticRunService(uow_factory).retry_failed(run_id)
        dispatcher = cast(Callable[..., None], request.app.state.monthly_semantic_dispatcher)
        if company_ids:
            dispatcher(run_id=run_id, run_company_ids=company_ids)
        dispatched = len(company_ids)
    return {"run_id": str(run_id), "status": "RUNNING", "dispatched": dispatched}


def _company_counter_key(value: MonitoringRunCompanyStatus) -> str | None:
    if value == MonitoringRunCompanyStatus.SUCCEEDED:
        return "succeeded"
    if value in {MonitoringRunCompanyStatus.BLOCKED, MonitoringRunCompanyStatus.NOT_RUN}:
        return "blocked"
    if value == MonitoringRunCompanyStatus.FAILED:
        return "failed"
    return None


def _delivery_status(
    now: datetime,
    deadline: datetime | None,
    output_ready: bool,
) -> str:
    if output_ready:
        return "COMPLETED"
    if deadline is None:
        return "ON_TRACK"
    if now > deadline:
        return "OVERDUE"
    if deadline - now <= timedelta(hours=12):
        return "AT_RISK"
    return "ON_TRACK"


def _queue_wait_seconds(
    batch_started: datetime | None,
    company_started: datetime | None,
    now: datetime,
) -> int:
    if batch_started is None:
        return 0
    endpoint = company_started or now
    return max(0, int((endpoint - batch_started).total_seconds()))


__all__ = ["router"]

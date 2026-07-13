from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tax_risk.persistence.risk_models import (
    AuditEvent,
    DetectionRecord,
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    ReviewAction,
    RiskCase,
)


class RiskRepository:
    """Run, detection, case, review, and audit persistence operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_run(self, run: MonitoringRun) -> None:
        self._session.add(run)

    def get_run(
        self,
        run_id: UUID,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> MonitoringRun | None:
        if for_update and for_share:
            raise ValueError("run lock cannot be both exclusive and shared")
        statement = select(MonitoringRun).where(MonitoringRun.id == run_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        elif for_share:
            statement = statement.with_for_update(read=True).execution_options(
                populate_existing=True
            )
        return self._session.scalar(statement)

    def get_run_by_key(
        self,
        run_key: str,
        *,
        for_update: bool = False,
    ) -> MonitoringRun | None:
        statement = select(MonitoringRun).where(MonitoringRun.run_key == run_key)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def add_run_company(self, run_company: MonitoringRunCompany) -> None:
        self._session.add(run_company)

    def get_run_company(
        self,
        run_company_id: UUID,
        *,
        for_update: bool = False,
    ) -> MonitoringRunCompany | None:
        statement = select(MonitoringRunCompany).where(
            MonitoringRunCompany.id == run_company_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def list_run_companies(
        self,
        run_id: UUID,
        *,
        statuses: Iterable[MonitoringRunCompanyStatus] | None = None,
        for_update: bool = False,
    ) -> list[MonitoringRunCompany]:
        statement = select(MonitoringRunCompany).where(
            MonitoringRunCompany.run_id == run_id
        )
        if statuses is not None:
            selected = tuple(set(statuses))
            if not selected:
                return []
            statement = statement.where(MonitoringRunCompany.status.in_(selected))
        statement = statement.order_by(MonitoringRunCompany.id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def run_company_status_counts(
        self,
        run_id: UUID,
    ) -> dict[MonitoringRunCompanyStatus, int]:
        rows = self._session.execute(
            select(MonitoringRunCompany.status, func.count())
            .where(MonitoringRunCompany.run_id == run_id)
            .group_by(MonitoringRunCompany.status)
        )
        return {status: count for status, count in rows}

    def add_detection(self, detection: DetectionRecord) -> None:
        self._session.add(detection)

    def add_case(self, risk_case: RiskCase) -> None:
        self._session.add(risk_case)

    def get_case_by_fingerprint(self, fingerprint: str) -> RiskCase | None:
        return self._session.scalar(
            select(RiskCase).where(RiskCase.fingerprint == fingerprint).with_for_update()
        )

    def get_case(
        self,
        case_id: UUID,
        *,
        for_update: bool = False,
    ) -> RiskCase | None:
        statement = select(RiskCase).where(RiskCase.id == case_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def add_review_action(self, review_action: ReviewAction) -> None:
        self._session.add(review_action)

    def add_audit_event(self, audit_event: AuditEvent) -> None:
        self._session.add(audit_event)


__all__ = ["RiskRepository"]

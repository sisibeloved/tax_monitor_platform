from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.risk_models import (
    AuditEvent,
    DetectionRecord,
    MonitoringRun,
    ReviewAction,
    RiskCase,
)


class RiskRepository:
    """Run, detection, case, review, and audit persistence operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_run(self, run: MonitoringRun) -> None:
        self._session.add(run)

    def add_detection(self, detection: DetectionRecord) -> None:
        self._session.add(detection)

    def add_case(self, risk_case: RiskCase) -> None:
        self._session.add(risk_case)

    def get_case_by_fingerprint(self, fingerprint: str) -> RiskCase | None:
        return self._session.scalar(
            select(RiskCase).where(RiskCase.fingerprint == fingerprint).with_for_update()
        )

    def add_review_action(self, review_action: ReviewAction) -> None:
        self._session.add(review_action)

    def add_audit_event(self, audit_event: AuditEvent) -> None:
        self._session.add(audit_event)


__all__ = ["RiskRepository"]

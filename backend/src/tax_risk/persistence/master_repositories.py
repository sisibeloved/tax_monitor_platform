from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.master_models import RuleVersion, TaxMasterVersion, VersionStatus


class MasterRepository:
    """Effective-dated master and rule-version persistence operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_tax_master(self, tax_master: TaxMasterVersion) -> None:
        self._session.add(tax_master)

    def published_tax_master(self, company_id: UUID, effective_on: date) -> TaxMasterVersion | None:
        statement = (
            select(TaxMasterVersion)
            .where(
                TaxMasterVersion.company_id == company_id,
                TaxMasterVersion.status == VersionStatus.PUBLISHED,
                TaxMasterVersion.valid_from <= effective_on,
                (TaxMasterVersion.valid_to.is_(None) | (TaxMasterVersion.valid_to >= effective_on)),
            )
            .order_by(
                TaxMasterVersion.valid_from.desc(),
                TaxMasterVersion.published_at.desc(),
                TaxMasterVersion.created_at.desc(),
                TaxMasterVersion.id.desc(),
            )
            .limit(1)
        )
        return self._session.scalar(statement)

    def add_rule_version(self, rule_version: RuleVersion) -> None:
        self._session.add(rule_version)


__all__ = ["MasterRepository"]

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

    def get_tax_master(
        self,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> TaxMasterVersion | None:
        statement = select(TaxMasterVersion).where(TaxMasterVersion.id == version_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def tax_masters_for_source_batch(self, source_batch_id: UUID) -> list[TaxMasterVersion]:
        return list(
            self._session.scalars(
                select(TaxMasterVersion)
                .where(TaxMasterVersion.source_batch_id == source_batch_id)
                .order_by(TaxMasterVersion.source_row_number, TaxMasterVersion.id)
            )
        )

    def published_tax_masters(
        self,
        company_id: UUID,
        effective_on: date,
    ) -> list[TaxMasterVersion]:
        return list(
            self._session.scalars(
                select(TaxMasterVersion)
                .where(
                    TaxMasterVersion.company_id == company_id,
                    TaxMasterVersion.status == VersionStatus.PUBLISHED,
                    TaxMasterVersion.valid_from <= effective_on,
                    (
                        TaxMasterVersion.valid_to.is_(None)
                        | (TaxMasterVersion.valid_to >= effective_on)
                    ),
                )
                .order_by(TaxMasterVersion.valid_from, TaxMasterVersion.id)
            )
        )

    def overlapping_published_tax_masters(
        self,
        candidate: TaxMasterVersion,
    ) -> list[TaxMasterVersion]:
        statement = select(TaxMasterVersion).where(
            TaxMasterVersion.company_id == candidate.company_id,
            TaxMasterVersion.id != candidate.id,
            TaxMasterVersion.status == VersionStatus.PUBLISHED,
            (
                TaxMasterVersion.valid_to.is_(None)
                | (TaxMasterVersion.valid_to >= candidate.valid_from)
            ),
        )
        if candidate.valid_to is not None:
            statement = statement.where(TaxMasterVersion.valid_from <= candidate.valid_to)
        return list(
            self._session.scalars(
                statement.order_by(TaxMasterVersion.valid_from, TaxMasterVersion.id).with_for_update()
            )
        )

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

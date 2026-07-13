from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tax_risk.domain.business_entertainment.company_scope import ScopeVersionStatus
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentScopeCompany,
    BusinessEntertainmentScopeVersion,
)
from tax_risk.persistence.ingest_models import Company


class BusinessEntertainmentScopeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_version(self, version: BusinessEntertainmentScopeVersion) -> None:
        self._session.add(version)

    def add_company(self, company: BusinessEntertainmentScopeCompany) -> None:
        self._session.add(company)

    def get_version(
        self,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> BusinessEntertainmentScopeVersion | None:
        statement = select(BusinessEntertainmentScopeVersion).where(
            BusinessEntertainmentScopeVersion.id == version_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def version_for_batch(
        self,
        batch_id: UUID,
    ) -> BusinessEntertainmentScopeVersion | None:
        return self._session.scalar(
            select(BusinessEntertainmentScopeVersion).where(
                BusinessEntertainmentScopeVersion.batch_id == batch_id
            )
        )

    def companies_for_version(self, version_id: UUID) -> list[Company]:
        return list(
            self._session.scalars(
                select(Company)
                .join(
                    BusinessEntertainmentScopeCompany,
                    BusinessEntertainmentScopeCompany.company_id == Company.id,
                )
                .where(BusinessEntertainmentScopeCompany.version_id == version_id)
                .order_by(Company.company_code)
            )
        )

    def published_for_date(
        self,
        effective_on: date,
        *,
        for_update: bool = False,
    ) -> list[BusinessEntertainmentScopeVersion]:
        statement = (
            select(BusinessEntertainmentScopeVersion)
            .where(
                BusinessEntertainmentScopeVersion.status == ScopeVersionStatus.PUBLISHED,
                BusinessEntertainmentScopeVersion.effective_from <= effective_on,
                BusinessEntertainmentScopeVersion.effective_to >= effective_on,
            )
            .order_by(BusinessEntertainmentScopeVersion.effective_from)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def overlapping_published(
        self,
        candidate: BusinessEntertainmentScopeVersion,
    ) -> list[BusinessEntertainmentScopeVersion]:
        return list(
            self._session.scalars(
                select(BusinessEntertainmentScopeVersion)
                .where(
                    BusinessEntertainmentScopeVersion.id != candidate.id,
                    BusinessEntertainmentScopeVersion.status == ScopeVersionStatus.PUBLISHED,
                    BusinessEntertainmentScopeVersion.effective_from <= candidate.effective_to,
                    BusinessEntertainmentScopeVersion.effective_to >= candidate.effective_from,
                )
                .order_by(BusinessEntertainmentScopeVersion.effective_from)
                .with_for_update()
            )
        )

    def lock_publication(self) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_namespace)"),
            {"lock_namespace": 2026071301},
        )

    def lock_import(self, source_batch_key: str) -> None:
        self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                ":lock_namespace, hashtext(:source_batch_key))"
            ),
            {
                "lock_namespace": 2026071302,
                "source_batch_key": source_batch_key,
            },
        )


__all__ = ["BusinessEntertainmentScopeRepository"]

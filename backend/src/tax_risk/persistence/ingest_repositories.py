from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tax_risk.persistence.ingest_models import (
    Company,
    IngestBatch,
    IngestError,
    SourceRecord,
)


class IngestRepository:
    """Company and append-only ingestion persistence operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_company(self, company: Company) -> None:
        self._session.add(company)

    def get_company(self, company_id: UUID) -> Company | None:
        return self._session.get(Company, company_id)

    def get_company_by_code(
        self,
        company_code: str,
        *,
        for_update: bool = False,
    ) -> Company | None:
        statement = select(Company).where(Company.company_code == company_code)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def lock_company_code(self, company_code: str) -> None:
        """Serialize even first-time inserts for one canonical company code."""

        self.lock_companies_exclusive({company_code})

    def lock_companies_exclusive(
        self,
        company_codes: Iterable[str],
    ) -> dict[str, Company | None]:
        return self._lock_companies(company_codes, shared=False)

    def lock_companies_shared(
        self,
        company_codes: Iterable[str],
    ) -> dict[str, Company | None]:
        return self._lock_companies(company_codes, shared=True)

    def _lock_companies(
        self,
        company_codes: Iterable[str],
        *,
        shared: bool,
    ) -> dict[str, Company | None]:
        ordered_codes = sorted(set(company_codes))
        advisory_function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        for company_code in ordered_codes:
            self._session.execute(
                text(
                    f"SELECT {advisory_function}("  # noqa: S608 - fixed function names only
                    ":lock_namespace, hashtext(:company_code))"
                ),
                {
                    "lock_namespace": 20260712,
                    "company_code": company_code,
                },
            )
        if not ordered_codes:
            return {}

        statement = (
            select(Company)
            .where(Company.company_code.in_(ordered_codes))
            .order_by(Company.company_code)
            .with_for_update(read=shared)
        )
        existing = {company.company_code: company for company in self._session.scalars(statement)}
        return {company_code: existing.get(company_code) for company_code in ordered_codes}

    def add_batch(self, batch: IngestBatch) -> None:
        self._session.add(batch)

    def get_batch(self, batch_id: UUID, *, for_update: bool = False) -> IngestBatch | None:
        statement = select(IngestBatch).where(IngestBatch.id == batch_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_batch_by_source_key(self, source: str, source_batch_key: str) -> IngestBatch | None:
        return self._session.scalar(
            select(IngestBatch).where(
                IngestBatch.source == source,
                IngestBatch.source_batch_key == source_batch_key,
            )
        )

    def add_error(self, error: IngestError) -> None:
        self._session.add(error)

    def list_errors(self, batch_id: UUID) -> list[IngestError]:
        return list(
            self._session.scalars(
                select(IngestError)
                .where(IngestError.batch_id == batch_id)
                .order_by(IngestError.row_number, IngestError.created_at, IngestError.id)
            )
        )

    def add_source_record(self, source_record: SourceRecord) -> None:
        self._session.add(source_record)


__all__ = ["IngestRepository"]

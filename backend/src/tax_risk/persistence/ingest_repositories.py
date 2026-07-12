from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.ingest_models import Company, IngestBatch, SourceRecord


class IngestRepository:
    """Company and append-only ingestion persistence operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_company(self, company: Company) -> None:
        self._session.add(company)

    def get_company(self, company_id: UUID) -> Company | None:
        return self._session.get(Company, company_id)

    def get_company_by_code(self, company_code: str) -> Company | None:
        return self._session.scalar(select(Company).where(Company.company_code == company_code))

    def add_batch(self, batch: IngestBatch) -> None:
        self._session.add(batch)

    def add_source_record(self, source_record: SourceRecord) -> None:
        self._session.add(source_record)


__all__ = ["IngestRepository"]

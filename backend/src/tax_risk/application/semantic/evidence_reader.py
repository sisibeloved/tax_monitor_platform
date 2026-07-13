"""Company-scoped semantic evidence access boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.ingest_models import Company, SourceRecord
from tax_risk.security.policies import Action, AuthorizationDenied, DEFAULT_POLICY
from tax_risk.security.principal import Principal


class EvidenceNotFound(LookupError):
    """The reference does not exist or is intentionally hidden by scope."""


@dataclass(frozen=True, slots=True)
class EvidenceProjection:
    reference_id: UUID
    company_id: UUID
    company_code: str
    dataset_code: str
    period: date
    amount: Decimal | None
    currency: str
    payload: dict[str, Any]


class EvidenceRepository(Protocol):
    def read_by_reference(self, reference_id: UUID) -> EvidenceProjection | None: ...


class SqlAlchemyEvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def read_by_reference(self, reference_id: UUID) -> EvidenceProjection | None:
        row = self._session.execute(
            select(SourceRecord, Company.company_code)
            .join(Company, Company.id == SourceRecord.company_id)
            .where(SourceRecord.id == reference_id)
        ).one_or_none()
        if row is None:
            return None
        source_record, company_code = row
        assert source_record.company_id is not None
        return EvidenceProjection(
            reference_id=source_record.id,
            company_id=source_record.company_id,
            company_code=company_code,
            dataset_code=source_record.dataset_code,
            period=source_record.period,
            amount=source_record.amount,
            currency=source_record.currency,
            payload=dict(source_record.payload),
        )


class EvidenceReader:
    def __init__(self, repository: EvidenceRepository) -> None:
        self._repository = repository

    def read_by_reference(
        self, principal: Principal, reference_id: UUID
    ) -> EvidenceProjection:
        action = Action.RUN_MONITOR if principal.is_service else Action.READ_RISK
        try:
            scope = DEFAULT_POLICY.company_scope(principal, action)
        except AuthorizationDenied as exc:
            raise EvidenceNotFound(str(reference_id)) from exc
        record = self._repository.read_by_reference(reference_id)
        if record is None or (scope is not None and record.company_id not in scope):
            raise EvidenceNotFound(str(reference_id))
        return record


__all__ = [
    "EvidenceNotFound",
    "EvidenceProjection",
    "EvidenceReader",
    "EvidenceRepository",
    "SqlAlchemyEvidenceRepository",
]

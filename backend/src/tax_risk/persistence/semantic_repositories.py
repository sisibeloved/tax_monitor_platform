from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.semantic_models import SapExpenseVoucherObservation


class SemanticRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_sap_observation(self, observation: SapExpenseVoucherObservation) -> None:
        self._session.add(observation)

    def sap_observations_for_batch(
        self,
        batch_id: UUID,
    ) -> list[SapExpenseVoucherObservation]:
        return list(
            self._session.scalars(
                select(SapExpenseVoucherObservation)
                .where(SapExpenseVoucherObservation.ingest_batch_id == batch_id)
                .order_by(SapExpenseVoucherObservation.source_record_key)
            )
        )


__all__ = ["SemanticRepository"]

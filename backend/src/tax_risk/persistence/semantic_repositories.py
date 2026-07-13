from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)
from tax_risk.persistence.semantic_models import (
    SapExpenseVoucherObservation,
    SapExpenseVoucherSnapshotProjection,
)
from tax_risk.persistence.snapshot_models import (
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
)
from tax_risk.persistence.ingest_models import Company


class SnapshotBoundSourceError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SemanticRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_sap_observation(self, observation: SapExpenseVoucherObservation) -> None:
        self._session.add(observation)

    def add_sap_projection(
        self,
        projection: SapExpenseVoucherSnapshotProjection,
    ) -> None:
        self._session.add(projection)

    def projected_observation_ids(self, snapshot_id: UUID) -> set[UUID]:
        return set(
            self._session.scalars(
                select(SapExpenseVoucherSnapshotProjection.observation_id).where(
                    SapExpenseVoucherSnapshotProjection.snapshot_id == snapshot_id
                )
            )
        )

    def snapshot_is_in_published_set(self, snapshot_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(SnapshotSetMember.id)
                .join(SnapshotSet, SnapshotSet.id == SnapshotSetMember.snapshot_set_id)
                .where(
                    SnapshotSetMember.snapshot_id == snapshot_id,
                    SnapshotSet.status == SnapshotSetStatus.PUBLISHED,
                )
                .limit(1)
            )
            is not None
        )

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

    def sap_observations_for_company_ytd(
        self,
        company_code: str,
        period_end: date,
    ) -> list[SapExpenseVoucherObservation]:
        return list(
            self._session.scalars(
                select(SapExpenseVoucherObservation)
                .where(
                    SapExpenseVoucherObservation.company_code == company_code,
                    SapExpenseVoucherObservation.fiscal_year == period_end.year,
                    SapExpenseVoucherObservation.period <= period_end.month,
                    SapExpenseVoucherObservation.account_family
                    == AccountFamily.BUSINESS_ENTERTAINMENT.value,
                )
                .order_by(
                    SapExpenseVoucherObservation.period,
                    SapExpenseVoucherObservation.document_number,
                    SapExpenseVoucherObservation.line_item,
                    SapExpenseVoucherObservation.id,
                )
            )
        )

    def load_snapshot_bound_sap_vouchers(
        self,
        snapshot_set_id: UUID,
        account_family: AccountFamily,
        company_code: str,
        period_end: date,
    ) -> list[SnapshotBoundSapExpenseVoucher]:
        snapshot_set = self._session.get(SnapshotSet, snapshot_set_id)
        if snapshot_set is None:
            raise SnapshotBoundSourceError(
                "SNAPSHOT_SET_NOT_FOUND", "snapshot set was not found"
            )
        if (
            snapshot_set.status != SnapshotSetStatus.PUBLISHED
            or snapshot_set.published_at is None
        ):
            raise SnapshotBoundSourceError(
                "SNAPSHOT_SET_NOT_PUBLISHED",
                "only a published snapshot set can supply evaluation input",
            )
        if snapshot_set.period != period_end:
            raise SnapshotBoundSourceError(
                "SNAPSHOT_PERIOD_MISMATCH", "period_end must equal the snapshot-set period"
            )

        statement = (
            select(SapExpenseVoucherSnapshotProjection, SapExpenseVoucherObservation)
            .join(
                SnapshotSetMember,
                SnapshotSetMember.snapshot_id
                == SapExpenseVoucherSnapshotProjection.snapshot_id,
            )
            .join(
                SapExpenseVoucherObservation,
                SapExpenseVoucherObservation.id
                == SapExpenseVoucherSnapshotProjection.observation_id,
            )
            .where(
                SnapshotSetMember.snapshot_set_id == snapshot_set_id,
                SapExpenseVoucherSnapshotProjection.company_code == company_code,
                SapExpenseVoucherSnapshotProjection.period == period_end,
                SapExpenseVoucherObservation.account_family == account_family.value,
                SapExpenseVoucherObservation.fiscal_year == period_end.year,
                SapExpenseVoucherObservation.period <= period_end.month,
            )
            .order_by(
                SapExpenseVoucherObservation.period,
                SapExpenseVoucherObservation.document_number,
                SapExpenseVoucherObservation.line_item,
                SapExpenseVoucherObservation.id,
            )
        )
        rows = self._session.execute(statement).all()
        if not rows:
            member_exists = self._session.scalar(
                select(SnapshotSetMember.id)
                .join(Company, Company.id == SnapshotSetMember.company_id)
                .where(
                    SnapshotSetMember.snapshot_set_id == snapshot_set_id,
                    Company.company_code == company_code,
                )
                .limit(1)
            )
            code = "SNAPSHOT_SOURCE_MISSING" if member_exists else "SNAPSHOT_MEMBER_MISSING"
            raise SnapshotBoundSourceError(
                code,
                "snapshot-bound SAP source is missing; absence is not treated as zero",
            )
        return [
            SnapshotBoundSapExpenseVoucher(
                company_code=observation.company_code,
                fiscal_year=observation.fiscal_year,
                period=observation.period,
                posting_date=observation.posting_date,
                document_number=observation.document_number,
                line_item=observation.line_item,
                current_account_code=observation.current_account_code,
                current_account_name=observation.current_account_name,
                amount=observation.amount,
                currency=observation.currency,
                summary=observation.summary,
                assignment=observation.assignment,
                reference=observation.reference,
                reversal_reference=observation.reversal_reference,
                account_family=AccountFamily(observation.account_family),
                projection_id=projection.id,
                snapshot_id=projection.snapshot_id,
                observation_id=observation.id,
                source_record_id=observation.source_record_id,
            )
            for projection, observation in rows
        ]


__all__ = ["SemanticRepository", "SnapshotBoundSourceError"]

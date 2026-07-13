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
    SemanticArtifactVersion,
    SemanticDetectionRecord,
    SemanticEvidenceTask,
    SemanticModelCallAudit,
    SuggestedAccountDictionaryVersion,
    SuggestedAccountEntry,
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

    def add_account_dictionary_version(
        self,
        version: SuggestedAccountDictionaryVersion,
    ) -> None:
        self._session.add(version)

    def add_suggested_account(self, entry: SuggestedAccountEntry) -> None:
        self._session.add(entry)

    def add_semantic_artifact(self, artifact: SemanticArtifactVersion) -> None:
        self._session.add(artifact)

    def get_semantic_artifact(
        self,
        artifact_id: UUID,
        *,
        for_update: bool = False,
    ) -> SemanticArtifactVersion | None:
        statement = select(SemanticArtifactVersion).where(
            SemanticArtifactVersion.id == artifact_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def overlapping_published_semantic_artifacts(
        self,
        artifact: SemanticArtifactVersion,
    ) -> list[SemanticArtifactVersion]:
        return list(
            self._session.scalars(
                select(SemanticArtifactVersion).where(
                    SemanticArtifactVersion.id != artifact.id,
                    SemanticArtifactVersion.artifact_type == artifact.artifact_type,
                    SemanticArtifactVersion.status == "PUBLISHED",
                    SemanticArtifactVersion.effective_from <= artifact.effective_to,
                    SemanticArtifactVersion.effective_to >= artifact.effective_from,
                )
            )
        )

    def active_semantic_artifacts(
        self,
        effective_on: date,
    ) -> list[SemanticArtifactVersion]:
        return list(
            self._session.scalars(
                select(SemanticArtifactVersion)
                .where(
                    SemanticArtifactVersion.status == "PUBLISHED",
                    SemanticArtifactVersion.effective_from <= effective_on,
                    SemanticArtifactVersion.effective_to >= effective_on,
                )
                .order_by(SemanticArtifactVersion.artifact_type)
            )
        )

    def add_model_call_audit(self, audit: SemanticModelCallAudit) -> None:
        self._session.add(audit)

    def add_semantic_detection(self, detection: SemanticDetectionRecord) -> None:
        self._session.add(detection)

    def get_semantic_detection_by_key(
        self,
        detection_key: str,
    ) -> SemanticDetectionRecord | None:
        return self._session.scalar(
            select(SemanticDetectionRecord).where(
                SemanticDetectionRecord.detection_key == detection_key
            )
        )

    def add_semantic_evidence_task(self, task: SemanticEvidenceTask) -> None:
        self._session.add(task)

    def get_evidence_task_for_detection(
        self,
        detection_id: UUID,
    ) -> SemanticEvidenceTask | None:
        return self._session.scalar(
            select(SemanticEvidenceTask).where(
                SemanticEvidenceTask.detection_id == detection_id
            )
        )

    def get_account_dictionary_version(
        self,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> SuggestedAccountDictionaryVersion | None:
        statement = select(SuggestedAccountDictionaryVersion).where(
            SuggestedAccountDictionaryVersion.id == version_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def get_account_dictionary_by_name(
        self,
        dictionary_version: str,
    ) -> SuggestedAccountDictionaryVersion | None:
        return self._session.scalar(
            select(SuggestedAccountDictionaryVersion).where(
                SuggestedAccountDictionaryVersion.dictionary_version == dictionary_version
            )
        )

    def get_account_dictionary_by_batch(
        self,
        batch_id: UUID,
    ) -> SuggestedAccountDictionaryVersion | None:
        return self._session.scalar(
            select(SuggestedAccountDictionaryVersion).where(
                SuggestedAccountDictionaryVersion.batch_id == batch_id
            )
        )

    def overlapping_published_account_dictionaries(
        self,
        version: SuggestedAccountDictionaryVersion,
    ) -> list[SuggestedAccountDictionaryVersion]:
        return list(
            self._session.scalars(
                select(SuggestedAccountDictionaryVersion).where(
                    SuggestedAccountDictionaryVersion.id != version.id,
                    SuggestedAccountDictionaryVersion.status == "PUBLISHED",
                    SuggestedAccountDictionaryVersion.effective_from <= version.effective_to,
                    SuggestedAccountDictionaryVersion.effective_to >= version.effective_from,
                )
            )
        )

    def get_suggested_account(
        self,
        version_id: UUID,
        account_id: str,
    ) -> SuggestedAccountEntry | None:
        return self._session.scalar(
            select(SuggestedAccountEntry).where(
                SuggestedAccountEntry.dictionary_version_id == version_id,
                SuggestedAccountEntry.account_id == account_id,
            )
        )

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

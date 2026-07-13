from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.limited_scope import DuplicateScopeMetric
from tax_risk.persistence.ingest_models import Company, SourceRecord
from tax_risk.persistence.semantic_models import (
    SapExpenseVoucherObservation,
    SapExpenseVoucherSnapshotProjection,
    SemanticArtifactVersion,
    SemanticDetectionRecord,
    SemanticEvidenceTask,
    SemanticModelCallAudit,
    SemanticVersionSetRecord,
    SuggestedAccountDictionaryVersion,
    SuggestedAccountEntry,
)
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
    SnapshotSource,
)


@dataclass(frozen=True, slots=True)
class ScopeFact:
    company_code: str
    period: str
    snapshot_set_id: UUID
    snapshot_id: UUID
    cumulative_expense: Decimal | None
    cumulative_base: Decimal | None


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

    def add_semantic_version_set(self, version_set: SemanticVersionSetRecord) -> None:
        self._session.add(version_set)

    def get_semantic_version_set(
        self,
        version_set_id: UUID,
        *,
        for_update: bool = False,
    ) -> SemanticVersionSetRecord | None:
        statement = select(SemanticVersionSetRecord).where(
            SemanticVersionSetRecord.id == version_set_id
        )
        if for_update:
            statement = statement.with_for_update(read=True).execution_options(
                populate_existing=True
            )
        return self._session.scalar(statement)

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

    def get_semantic_detection(
        self,
        detection_id: UUID,
    ) -> SemanticDetectionRecord | None:
        return self._session.get(SemanticDetectionRecord, detection_id)

    def sap_observation_by_source_record(
        self,
        source_record_id: UUID,
    ) -> SapExpenseVoucherObservation | None:
        return self._session.scalar(
            select(SapExpenseVoucherObservation).where(
                SapExpenseVoucherObservation.source_record_id == source_record_id
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
        *,
        batch_ids: tuple[UUID, ...] | None = None,
        account_families: tuple[AccountFamily, ...] = (
            AccountFamily.BUSINESS_ENTERTAINMENT,
        ),
    ) -> list[SapExpenseVoucherObservation]:
        statement = select(SapExpenseVoucherObservation).where(
            SapExpenseVoucherObservation.company_code == company_code,
            SapExpenseVoucherObservation.fiscal_year == period_end.year,
            SapExpenseVoucherObservation.period <= period_end.month,
            SapExpenseVoucherObservation.account_family.in_(
                family.value for family in account_families
            )
        )
        if batch_ids is not None:
            statement = statement.where(
                SapExpenseVoucherObservation.ingest_batch_id.in_(batch_ids)
            )
        return list(
            self._session.scalars(
                statement.order_by(
                    SapExpenseVoucherObservation.posting_date,
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
        *,
        allow_empty: bool = False,
    ) -> list[SnapshotBoundSapExpenseVoucher]:
        member = self._require_published_member(
            snapshot_set_id=snapshot_set_id,
            snapshot_id=None,
            company_code=company_code,
            period_end=period_end,
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
                SnapshotSetMember.id == member.id,
                SapExpenseVoucherSnapshotProjection.snapshot_id == member.snapshot_id,
                SapExpenseVoucherSnapshotProjection.company_code == company_code,
                SapExpenseVoucherSnapshotProjection.period == period_end,
                SapExpenseVoucherObservation.account_family == account_family.value,
                SapExpenseVoucherObservation.fiscal_year == period_end.year,
                SapExpenseVoucherObservation.period <= period_end.month,
            )
            .order_by(
                SapExpenseVoucherObservation.posting_date,
                SapExpenseVoucherObservation.document_number,
                SapExpenseVoucherObservation.line_item,
                SapExpenseVoucherObservation.id,
            )
        )
        rows = self._session.execute(statement).all()
        if not rows:
            if allow_empty:
                return []
            raise SnapshotBoundSourceError(
                "SNAPSHOT_SOURCE_MISSING",
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

    def _require_published_member(
        self,
        *,
        snapshot_set_id: UUID,
        snapshot_id: UUID | None,
        company_code: str,
        period_end: date,
    ) -> SnapshotSetMember:
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
            select(SnapshotSetMember)
            .join(Company, Company.id == SnapshotSetMember.company_id)
            .join(AccountingSnapshot, AccountingSnapshot.id == SnapshotSetMember.snapshot_id)
            .where(
                SnapshotSetMember.snapshot_set_id == snapshot_set_id,
                Company.company_code == company_code,
                AccountingSnapshot.period == period_end,
            )
        )
        if snapshot_id is not None:
            statement = statement.where(SnapshotSetMember.snapshot_id == snapshot_id)
        member = self._session.scalar(statement)
        if member is None:
            raise SnapshotBoundSourceError(
                "SNAPSHOT_MEMBER_MISSING",
                "the published snapshot set has no exact member for the company",
            )
        return member


class MonthlySemanticRepository:
    METRICS = {
        MonitorType.WELFARE: ("WELFARE_YTD", "SALARY_YTD"),
        MonitorType.DONATION: ("DONATION_YTD", "PROFIT_YTD"),
    }

    def __init__(self, session: Session, semantic: SemanticRepository | None = None) -> None:
        self._session = session
        self._semantic = semantic or SemanticRepository(session)

    def get_scope_fact(
        self,
        company_code: str,
        period: str,
        monitoring_type: MonitorType,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
    ) -> ScopeFact:
        period_end = _month_end(period)
        member = self._semantic._require_published_member(
            snapshot_set_id=snapshot_set_id,
            snapshot_id=snapshot_id,
            company_code=company_code,
            period_end=period_end,
        )
        expense_code, base_code = self.METRICS[monitoring_type]
        rows = self._session.execute(
            select(SourceRecord.dataset_code, SourceRecord.amount)
            .join(
                SnapshotSource,
                SnapshotSource.ingest_batch_id == SourceRecord.batch_id,
            )
            .where(
                SnapshotSource.snapshot_id == member.snapshot_id,
                SourceRecord.company_id == member.company_id,
                SourceRecord.period == period_end,
                SourceRecord.dataset_code.in_((expense_code, base_code)),
            )
            .order_by(SourceRecord.dataset_code, SourceRecord.id)
        ).all()
        grouped: dict[str, list[Decimal]] = {expense_code: [], base_code: []}
        for metric_code, amount in rows:
            if amount is not None:
                grouped[metric_code].append(amount)
        duplicates = tuple(code for code, values in grouped.items() if len(values) > 1)
        if duplicates:
            raise DuplicateScopeMetric(
                f"duplicate monthly scope metric: {', '.join(sorted(duplicates))}"
            )
        return ScopeFact(
            company_code=company_code,
            period=period,
            snapshot_set_id=snapshot_set_id,
            snapshot_id=member.snapshot_id,
            cumulative_expense=grouped[expense_code][0] if grouped[expense_code] else None,
            cumulative_base=grouped[base_code][0] if grouped[base_code] else None,
        )

    def load_snapshot_bound_sap_vouchers(
        self,
        *,
        snapshot_set_id: UUID,
        account_family: AccountFamily,
        company_code: str,
        period_end: date,
    ) -> list[SnapshotBoundSapExpenseVoucher]:
        return self._semantic.load_snapshot_bound_sap_vouchers(
            snapshot_set_id,
            account_family,
            company_code,
            period_end,
            allow_empty=True,
        )


def _month_end(period: str) -> date:
    try:
        year_text, month_text = period.split("-", maxsplit=1)
        year, month = int(year_text), int(month_text)
        return date(year, month, monthrange(year, month)[1])
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("period must use YYYY-MM format") from error


__all__ = [
    "MonthlySemanticRepository",
    "ScopeFact",
    "SemanticRepository",
    "SnapshotBoundSourceError",
]

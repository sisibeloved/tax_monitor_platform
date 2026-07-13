from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.domain.cases import MonitorType
from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class SapExpenseVoucherObservation(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "sap_expense_voucher_observation"
    __table_args__ = (
        UniqueConstraint(
            "ingest_batch_id",
            "source_record_key",
            name="uq_sap_obs_batch_key",
        ),
        UniqueConstraint(
            "ingest_batch_id",
            "company_code",
            "fiscal_year",
            "document_number",
            "line_item",
            name="uq_sap_obs_batch_business_key",
        ),
        CheckConstraint("period BETWEEN 1 AND 12", name="period"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint(
            "account_family IN ('BUSINESS_ENTERTAINMENT', 'WELFARE', 'DONATION')",
            name="account_family",
        ),
        Index("ix_sap_obs_batch", "ingest_batch_id"),
        Index("ix_sap_obs_company", "company_code"),
    )

    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_sap_obs_source", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    ingest_batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", name="fk_sap_obs_batch", ondelete="RESTRICT"),
        nullable=False,
    )
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_number: Mapped[str] = mapped_column(String(64), nullable=False)
    line_item: Mapped[str] = mapped_column(String(32), nullable=False)
    current_account_code: Mapped[str] = mapped_column(String(64), nullable=False)
    current_account_name: Mapped[str] = mapped_column(String(256), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    assignment: Mapped[str | None] = mapped_column(String(256))
    reference: Mapped[str | None] = mapped_column(String(256))
    reversal_reference: Mapped[str | None] = mapped_column(String(256))
    account_family: Mapped[str] = mapped_column(String(64), nullable=False)


class SapExpenseVoucherSnapshotProjection(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "sap_expense_voucher_snapshot_projection"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "observation_id", name="uq_sap_projection_snapshot_obs"),
        Index("ix_sap_projection_obs", "observation_id"),
        Index("ix_sap_projection_snapshot", "snapshot_id"),
    )

    observation_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "sap_expense_voucher_observation.id",
            name="fk_sap_projection_obs",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "accounting_snapshot.id",
            name="fk_sap_projection_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)


class SuggestedAccountDictionaryVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "suggested_account_dictionary_version"
    __table_args__ = (
        UniqueConstraint("dictionary_version", name="uq_account_dict_version"),
        CheckConstraint("effective_to >= effective_from", name="effective_period"),
        CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="status",
        ),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
        CheckConstraint(
            "(status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL "
            "AND published_at IS NULL) OR "
            "(status = 'APPROVED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NULL) OR "
            "(status = 'PUBLISHED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NOT NULL AND published_by IS NOT NULL) OR "
            "status = 'RETIRED'",
            name="lifecycle",
        ),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", name="fk_account_dict_batch", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    dictionary_version: Mapped[str] = mapped_column(String(128), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(256))
    published_by: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SuggestedAccountEntry(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "suggested_account_entry"
    __table_args__ = (
        UniqueConstraint("dictionary_version_id", "account_id", name="uq_account_entry_id"),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="status"),
        Index("ix_account_entry_version", "dictionary_version_id"),
    )

    dictionary_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "suggested_account_dictionary_version.id",
            name="fk_account_entry_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_account_entry_source", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_code: Mapped[str] = mapped_column(String(64), nullable=False)
    account_name: Mapped[str] = mapped_column(String(256), nullable=False)
    accounting_classification: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_monitor_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    allowed_labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class SemanticArtifactVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "semantic_artifact_version"
    __table_args__ = (
        UniqueConstraint("artifact_type", "version", name="uq_semantic_artifact_version"),
        CheckConstraint(
            "artifact_type IN ('MODEL', 'PROMPT', 'CASE_LIBRARY')",
            name="artifact_type",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="status",
        ),
        CheckConstraint("effective_to >= effective_from", name="effective_period"),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
        CheckConstraint(
            "(status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL "
            "AND published_at IS NULL) OR "
            "(status = 'APPROVED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NULL) OR "
            "(status = 'PUBLISHED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NOT NULL AND published_by IS NOT NULL) OR "
            "status = 'RETIRED'",
            name="lifecycle",
        ),
        Index("ix_semantic_artifact_effective", "artifact_type", "status", "effective_from"),
    )

    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(String(256))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(256))
    published_by: Mapped[str | None] = mapped_column(String(256))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SemanticModelCallAudit(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "semantic_model_call_audit"
    __table_args__ = (
        CheckConstraint("length(request_checksum) = 64", name="request_checksum_length"),
        CheckConstraint("length(output_checksum) = 64", name="output_checksum_length"),
        CheckConstraint("token_count >= 0", name="nonnegative_tokens"),
        CheckConstraint("latency_ms >= 0", name="nonnegative_latency"),
        CheckConstraint("retry_count >= 0", name="nonnegative_retries"),
        Index("ix_model_call_candidate", "candidate_key"),
        Index("ix_model_call_run", "run_id"),
    )

    candidate_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_library_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    output_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_status: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retention_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    operator_id: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticVersionSetRecord(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "semantic_version_set"
    __table_args__ = (
        UniqueConstraint("set_key", name="uq_semantic_version_set_key"),
        CheckConstraint("effective_to >= effective_from", name="effective_period"),
        CheckConstraint("status IN ('PUBLISHED', 'RETIRED')", name="status"),
        CheckConstraint("length(set_key) = 64", name="set_key_length"),
    )

    set_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "rule_version.id",
            name="fk_semantic_version_set_rule_version_id_rule_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    model_artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "semantic_artifact_version.id",
            name="fk_sem_version_set_model_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    prompt_artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "semantic_artifact_version.id",
            name="fk_sem_version_set_prompt_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    case_library_artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "semantic_artifact_version.id",
            name="fk_sem_version_set_case_library_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    account_dictionary_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "suggested_account_dictionary_version.id",
            name="fk_sem_version_set_account_dictionary",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class SemanticDetectionRecord(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "semantic_detection_record"
    __table_args__ = (
        UniqueConstraint("detection_key", name="uq_semantic_detection_key"),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 9999", name="fiscal_year"),
        CheckConstraint("period BETWEEN 1 AND 12", name="period"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint(
            "source_mode IN ('SAP_LINKED', 'BUSINESS_DOCUMENT_UNLINKED')",
            name="source_mode",
        ),
        CheckConstraint(
            "(source_mode = 'SAP_LINKED' AND sap_observation_id IS NOT NULL "
            "AND sap_document_number IS NOT NULL AND sap_line_item IS NOT NULL) OR "
            "(source_mode = 'BUSINESS_DOCUMENT_UNLINKED' AND sap_observation_id IS NULL "
            "AND sap_document_number IS NULL AND sap_line_item IS NULL)",
            name="sap_identity",
        ),
        Index("ix_semantic_detection_candidate", "candidate_key"),
        Index("ix_semantic_detection_company_period", "company_code", "fiscal_year", "period"),
    )

    detection_key: Mapped[str] = mapped_column(String(512), nullable=False)
    candidate_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    monitoring_type: Mapped[MonitorType] = mapped_column(
        Enum(MonitorType, name="monitor_type", create_type=False),
        nullable=False,
        server_default="BUSINESS_ENTERTAINMENT",
    )
    source_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_sem_detection_source", ondelete="RESTRICT"),
        nullable=False,
    )
    sap_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "sap_expense_voucher_observation.id",
            name="fk_sem_detection_sap",
            ondelete="RESTRICT",
        )
    )
    sap_document_number: Mapped[str | None] = mapped_column(String(64))
    sap_line_item: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "accounting_snapshot.id",
            name="fk_sem_detection_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    exact_evidence_link_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "evidence_link.id",
            name="fk_sem_detection_evidence_link",
            ondelete="RESTRICT",
        )
    )
    rule_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_library_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_dictionary_version: Mapped[str] = mapped_column(String(128), nullable=False)
    semantic_label: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    recommended_account_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    rationale_summary: Mapped[str] = mapped_column(Text, nullable=False)
    missing_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticEvidenceTask(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "semantic_evidence_task"
    __table_args__ = (
        UniqueConstraint("detection_id", name="uq_semantic_evidence_task_detection"),
        CheckConstraint("status IN ('OPEN', 'COMPLETED', 'CANCELLED')", name="status"),
        Index("ix_semantic_evidence_task_company", "company_code", "status"),
    )

    detection_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "semantic_detection_record.id",
            name="fk_sem_evidence_task_detection",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    missing_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "SapExpenseVoucherObservation",
    "SapExpenseVoucherSnapshotProjection",
    "SuggestedAccountDictionaryVersion",
    "SuggestedAccountEntry",
    "SemanticArtifactVersion",
    "SemanticModelCallAudit",
    "SemanticVersionSetRecord",
    "SemanticDetectionRecord",
    "SemanticEvidenceTask",
]

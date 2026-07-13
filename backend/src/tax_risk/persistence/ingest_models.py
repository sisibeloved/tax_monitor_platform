from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class CompanyLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class IngestBatchStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class IngestMode(StrEnum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class Company(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "company"
    __table_args__ = (
        CheckConstraint(
            "(lifecycle = 'ACTIVE' AND deactivated_at IS NULL) OR "
            "(lifecycle = 'INACTIVE' AND deactivated_at IS NOT NULL)",
            name="lifecycle_audit",
        ),
    )

    company_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    lifecycle: Mapped[CompanyLifecycle] = mapped_column(
        Enum(CompanyLifecycle, name="company_lifecycle"),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    lifecycle_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    master_data_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_reason: Mapped[str | None] = mapped_column(Text)
    lifecycle_changed_by: Mapped[str | None] = mapped_column(String(256))


class IngestBatch(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "ingest_batch"
    __table_args__ = (
        UniqueConstraint("source", "source_batch_key", name="uq_ingest_batch_source_key"),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint(
            "record_count >= 0 AND accepted_count >= 0 AND rejected_count >= 0",
            name="nonnegative_counts",
        ),
        CheckConstraint(
            "accepted_count + rejected_count = record_count",
            name="reconciled_counts",
        ),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
    )

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_batch_key: Mapped[str] = mapped_column(String(256), nullable=False)
    dataset_code: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[IngestBatchStatus] = mapped_column(
        Enum(IngestBatchStatus, name="ingest_batch_status"), nullable=False
    )
    extraction_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    mode: Mapped[IngestMode] = mapped_column(Enum(IngestMode, name="ingest_mode"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(Text)
    source_primary_key_definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    control_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class IngestError(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "ingest_error"
    __table_args__ = (CheckConstraint("row_number > 0", name="positive_row_number"),)

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class SourceRecord(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_record_key", name="uq_source_record_batch_key"),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint(
            "amount IS NOT NULL OR dataset_code = 'oa_material_requisition'",
            name="amount_required_by_dataset",
        ),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), index=True
    )
    dataset_code: Mapped[str] = mapped_column(String(128), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "Company",
    "CompanyLifecycle",
    "IngestBatch",
    "IngestBatchStatus",
    "IngestError",
    "IngestMode",
    "SourceRecord",
]

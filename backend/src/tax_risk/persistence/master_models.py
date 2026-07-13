from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class VersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class TaxMasterVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "tax_master_version"
    __table_args__ = (
        UniqueConstraint("id", "company_id", name="uq_tax_master_id_company"),
        UniqueConstraint(
            "company_id", "valid_from", "version", name="uq_tax_master_company_effective_version"
        ),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="valid_period"),
        CheckConstraint("tax_rate BETWEEN 0 AND 1", name="tax_rate"),
        CheckConstraint("loss_carryforward >= 0", name="nonnegative_loss_carryforward"),
        CheckConstraint(
            "average_tax_burden_rate_3y BETWEEN 0 AND 1", name="average_tax_burden_rate"
        ),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("source_row_number > 1", name="positive_source_row_number"),
        CheckConstraint(
            "source_checksum IS NULL OR length(source_checksum) = 64",
            name="source_checksum_length",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint(
            "(status = 'DRAFT' AND published_at IS NULL AND approved_by IS NULL) OR "
            "(status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL "
            "AND approved_by IS NOT NULL AND btrim(approved_by) <> '')",
            name="published_at_state",
        ),
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"), nullable=False
    )
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)
    loss_carryforward: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    average_tax_burden_rate_3y: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_file_name: Mapped[str | None] = mapped_column(Text)
    source_checksum: Mapped[str | None] = mapped_column(String(64))
    source_row_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    uploaded_by: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(256))


class RuleVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "rule_version"
    __table_args__ = (
        UniqueConstraint("rule_code", "version", name="uq_rule_version_code_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="valid_period"
        ),
        CheckConstraint(
            "(status = 'DRAFT' AND published_at IS NULL) OR "
            "(status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL)",
            name="published_at_state",
        ),
    )

    rule_code: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status", create_type=False), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(256))


__all__ = ["RuleVersion", "TaxMasterVersion", "VersionStatus"]

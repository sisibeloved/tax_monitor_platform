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
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class SnapshotStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"


class SnapshotSetStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"


class AccountingSnapshot(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "accounting_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tax_master_version_id", "company_id"],
            ["tax_master_version.id", "tax_master_version.company_id"],
            name="fk_accounting_snapshot_master_company",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "company_id",
            "tax_master_version_id",
            name="uq_accounting_snapshot_id_company_master",
        ),
        UniqueConstraint(
            "company_id",
            "period",
            "source_version_set_hash",
            name="uq_accounting_snapshot_company_period_sources",
        ),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("record_count >= 0", name="record_count"),
        CheckConstraint("length(source_version_set_hash) = 64", name="source_version_hash_length"),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
        CheckConstraint(
            "(status IN ('DRAFT', 'VALIDATED') AND published_at IS NULL) OR "
            "(status = 'PUBLISHED' AND published_at IS NOT NULL)",
            name="published_at_state",
        ),
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tax_master_version_id: Mapped[UUID] = mapped_column(nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    source_version_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SnapshotStatus] = mapped_column(
        Enum(SnapshotStatus, name="snapshot_status"),
        nullable=False,
        server_default=text("'DRAFT'"),
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    control_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SnapshotSource(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "snapshot_source"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "ingest_batch_id", name="uq_snapshot_source_snapshot_batch"
        ),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("record_count >= 0", name="record_count"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounting_snapshot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ingest_batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    control_total: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SnapshotSet(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "snapshot_set"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint("expected_member_count >= 100", name="minimum_member_count"),
        CheckConstraint(
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(status <> 'PUBLISHED' AND published_at IS NULL)",
            name="published_at_state",
        ),
    )

    set_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SnapshotSetStatus] = mapped_column(
        Enum(SnapshotSetStatus, name="snapshot_set_status"),
        nullable=False,
        server_default=text("'DRAFT'"),
    )
    expected_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
    )
    supersedes_snapshot_set_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("snapshot_set.id", ondelete="RESTRICT")
    )


class SnapshotSetMember(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "snapshot_set_member"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_set_id", "company_id", name="uq_snapshot_set_member_set_company"
        ),
        UniqueConstraint(
            "snapshot_set_id", "snapshot_id", name="uq_snapshot_set_member_set_snapshot"
        ),
        Index("ix_snapshot_set_member_set_id", "snapshot_set_id"),
    )

    snapshot_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("snapshot_set.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounting_snapshot.id", ondelete="RESTRICT"), nullable=False
    )


__all__ = [
    "AccountingSnapshot",
    "SnapshotSet",
    "SnapshotSetMember",
    "SnapshotSetStatus",
    "SnapshotSource",
    "SnapshotStatus",
]

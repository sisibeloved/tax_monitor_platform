from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

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
            "account_family = 'BUSINESS_ENTERTAINMENT'",
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


__all__ = [
    "SapExpenseVoucherObservation",
    "SapExpenseVoucherSnapshotProjection",
]

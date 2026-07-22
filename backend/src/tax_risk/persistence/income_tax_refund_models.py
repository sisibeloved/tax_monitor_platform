from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class SapRefundEvidenceBatch(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """Completed SAP extract proving coverage through a monthly scan period."""

    __tablename__ = "sap_refund_evidence_batch"
    __table_args__ = (
        UniqueConstraint("source_batch_key", name="uq_sap_refund_evidence_batch_source_key"),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 9999", name="fiscal_year"),
        CheckConstraint(
            "EXTRACT(YEAR FROM through_period) = fiscal_year",
            name="through_period_year",
        ),
        CheckConstraint(
            "jsonb_typeof(company_ids) = 'array' AND jsonb_array_length(company_ids) > 0",
            name="company_ids",
        ),
        CheckConstraint("status = 'COMPLETE'", name="status"),
        CheckConstraint("record_count >= 0", name="nonnegative_record_count"),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
        Index("ix_sap_refund_evidence_period", "fiscal_year", "through_period"),
    )

    source_batch_key: Mapped[str] = mapped_column(String(256), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    through_period: Mapped[date] = mapped_column(Date, nullable=False)
    company_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class IncomeTaxRefundTarget(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """One controlled annual refund target for a company."""

    __tablename__ = "income_tax_refund_target"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "refund_tax_year",
            name="uq_income_tax_refund_target_company_year",
        ),
        UniqueConstraint(
            "id",
            "company_id",
            name="uq_income_tax_refund_target_id_company",
        ),
        CheckConstraint("refund_tax_year BETWEEN 2000 AND 9999", name="refund_tax_year"),
        CheckConstraint("expected_amount > 0", name="positive_expected_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("btrim(source_record_key) <> ''", name="source_record_key"),
        CheckConstraint("btrim(source_version) <> ''", name="source_version"),
        CheckConstraint("receipt_status IN ('PENDING', 'RECEIVED')", name="receipt_status"),
        CheckConstraint(
            "(receipt_status = 'PENDING' AND received_at IS NULL) OR "
            "(receipt_status = 'RECEIVED' AND received_at IS NOT NULL)",
            name="receipt_state",
        ),
        Index("ix_income_tax_refund_target_status", "receipt_status", "latest_scan_period"),
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", name="fk_refund_target_company", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    refund_tax_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    receipt_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_scan_period: Mapped[date | None] = mapped_column(Date)


class SapGlLineObservation(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """Canonical SAP G/L line used as deterministic refund evidence."""

    __tablename__ = "sap_gl_line_observation"
    __table_args__ = (
        UniqueConstraint(
            "source_batch_key",
            "client",
            "ledger",
            "company_id",
            "fiscal_year",
            "document_number",
            "line_item",
            name="uq_sap_gl_line_observation_business_key",
        ),
        UniqueConstraint(
            "id",
            "company_id",
            name="uq_sap_gl_line_observation_id_company",
        ),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 9999", name="fiscal_year"),
        CheckConstraint("fiscal_period BETWEEN 1 AND 12", name="fiscal_period"),
        CheckConstraint(
            "EXTRACT(YEAR FROM posting_date) = fiscal_year "
            "AND EXTRACT(MONTH FROM posting_date) = fiscal_period",
            name="posting_period",
        ),
        CheckConstraint(
            "account_category IN ('INCOME_TAX_EXPENSE', 'OTHER_INCOME', 'TAXES_PAYABLE')",
            name="account_category",
        ),
        CheckConstraint("debit_credit IN ('DEBIT', 'CREDIT')", name="debit_credit"),
        CheckConstraint("amount >= 0", name="nonnegative_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("length(source_hash) = 64", name="source_hash_length"),
        Index(
            "ix_sap_refund_gl_match",
            "company_id",
            "fiscal_year",
            "fiscal_period",
            "account_category",
            "debit_credit",
            "currency",
            "amount",
        ),
        Index("ix_sap_refund_gl_source_batch", "source_batch_key"),
    )

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", name="fk_sap_refund_gl_company", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_batch_key: Mapped[str] = mapped_column(
        String(256),
        ForeignKey(
            "sap_refund_evidence_batch.source_batch_key",
            name="fk_sap_refund_gl_evidence_batch",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    client: Mapped[str] = mapped_column(String(32), nullable=False)
    ledger: Mapped[str] = mapped_column(String(32), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_number: Mapped[str] = mapped_column(String(64), nullable=False)
    line_item: Mapped[str] = mapped_column(String(32), nullable=False)
    gl_account_code: Mapped[str] = mapped_column(String(64), nullable=False)
    gl_account_name: Mapped[str] = mapped_column(String(256), nullable=False)
    account_category: Mapped[str] = mapped_column(String(32), nullable=False)
    debit_credit: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class IncomeTaxRefundScanResult(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """Monthly classification of one annual refund target."""

    __tablename__ = "income_tax_refund_scan_result"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "scan_period",
            name="uq_income_tax_refund_scan_target_period",
        ),
        CheckConstraint(
            "receipt_status IN ('NOT_RECEIVED', 'RECEIVED', 'AMBIGUOUS')",
            name="receipt_status",
        ),
        CheckConstraint(
            "account_status IN ('NOT_APPLICABLE', 'CORRECT', 'WRONG_ACCOUNT', 'AMBIGUOUS')",
            name="account_status",
        ),
        CheckConstraint("expected_amount > 0", name="positive_expected_amount"),
        CheckConstraint(
            "matched_amount IS NULL OR matched_amount > 0",
            name="positive_matched_amount",
        ),
        CheckConstraint(
            "(gl_account_code IS NULL AND gl_account_name IS NULL) OR "
            "(gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL)",
            name="account_detail_pair",
        ),
        CheckConstraint(
            "jsonb_typeof(structured_output) = 'object'",
            name="structured_output_object",
        ),
        CheckConstraint(
            "structured_output -> 'completeness' = 'true'::jsonb "
            "AND jsonb_typeof(structured_output -> 'source_batch_key') = 'string' "
            "AND btrim(structured_output ->> 'source_batch_key') <> ''",
            name="source_completeness",
        ),
        CheckConstraint(
            "(receipt_status = 'NOT_RECEIVED' AND account_status = 'NOT_APPLICABLE' "
            "AND matched_line_id IS NULL AND matched_amount IS NULL "
            "AND gl_account_code IS NULL AND gl_account_name IS NULL AND alert_code IS NULL) OR "
            "(receipt_status = 'RECEIVED' AND account_status = 'CORRECT' "
            "AND matched_line_id IS NOT NULL AND matched_amount = expected_amount "
            "AND gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL "
            "AND alert_code IS NULL) OR "
            "(receipt_status = 'RECEIVED' AND account_status = 'WRONG_ACCOUNT' "
            "AND matched_line_id IS NOT NULL AND matched_amount = expected_amount "
            "AND gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL "
            "AND alert_code = 'REFUND_BOOKED_TO_WRONG_ACCOUNT') OR "
            "(receipt_status = 'AMBIGUOUS' AND account_status = 'AMBIGUOUS' "
            "AND matched_line_id IS NULL AND matched_amount IS NULL "
            "AND gl_account_code IS NULL AND gl_account_name IS NULL AND alert_code IS NULL)",
            name="classification_state",
        ),
        ForeignKeyConstraint(
            ["target_id", "company_id"],
            ["income_tax_refund_target.id", "income_tax_refund_target.company_id"],
            name="fk_refund_scan_target_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["matched_line_id", "company_id"],
            ["sap_gl_line_observation.id", "sap_gl_line_observation.company_id"],
            name="fk_refund_scan_matched_line_company",
            ondelete="RESTRICT",
        ),
        Index("ix_income_tax_refund_scan_company_period", "company_id", "scan_period"),
    )

    target_id: Mapped[UUID] = mapped_column(nullable=False)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    scan_period: Mapped[date] = mapped_column(Date, nullable=False)
    receipt_status: Mapped[str] = mapped_column(String(32), nullable=False)
    account_status: Mapped[str] = mapped_column(String(32), nullable=False)
    matched_line_id: Mapped[UUID | None] = mapped_column(nullable=True)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    matched_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    gl_account_code: Mapped[str | None] = mapped_column(String(64))
    gl_account_name: Mapped[str | None] = mapped_column(String(256))
    alert_code: Mapped[str | None] = mapped_column(String(128))
    structured_output: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )


class IncomeTaxRefundWriteback(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """Transactional outbox row for the single received-status writeback per target."""

    __tablename__ = "income_tax_refund_writeback"
    __table_args__ = (
        UniqueConstraint("target_id", name="uq_income_tax_refund_writeback_target"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_income_tax_refund_writeback_idempotency_key",
        ),
        CheckConstraint("btrim(idempotency_key) <> ''", name="idempotency_key"),
        CheckConstraint("btrim(desired_value) <> ''", name="desired_value"),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint(
            "(status = 'PENDING' AND processed_at IS NULL) OR "
            "(status = 'PROCESSING' AND attempt_count > 0 AND processed_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND attempt_count > 0 AND processed_at IS NOT NULL "
            "AND last_error IS NULL) OR "
            "(status = 'FAILED' AND attempt_count > 0 AND processed_at IS NULL "
            "AND last_error IS NOT NULL AND btrim(last_error) <> '')",
            name="delivery_state",
        ),
        ForeignKeyConstraint(
            ["target_id", "company_id"],
            ["income_tax_refund_target.id", "income_tax_refund_target.company_id"],
            name="fk_refund_writeback_target_company",
            ondelete="RESTRICT",
        ),
        Index("ix_income_tax_refund_writeback_status", "status", "created_at"),
    )

    target_id: Mapped[UUID] = mapped_column(nullable=False)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    desired_value: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'PENDING'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "IncomeTaxRefundScanResult",
    "IncomeTaxRefundTarget",
    "IncomeTaxRefundWriteback",
    "SapGlLineObservation",
    "SapRefundEvidenceBatch",
]

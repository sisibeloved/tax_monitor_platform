"""Add income-tax refund receipt and account-accuracy persistence.

Revision ID: 0020_income_tax_refund_accuracy
Revises: 0019_deferred_tax_accuracy
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0020_income_tax_refund_accuracy"
down_revision: str | None = "0019_deferred_tax_accuracy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DIRECT_COMPANY_TABLES = (
    "income_tax_refund_target",
    "sap_gl_line_observation",
    "income_tax_refund_scan_result",
    "income_tax_refund_writeback",
)


def upgrade() -> None:
    # PostgreSQL requires a commit before a newly added enum value can be used.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE monitor_type ADD VALUE IF NOT EXISTS 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY'"
        )

    _create_evidence_batch()
    _create_refund_target()
    _create_sap_gl_line()
    _create_scan_result()
    _create_writeback()
    _enable_rls()


def downgrade() -> None:
    op.drop_table("income_tax_refund_writeback")
    op.drop_table("income_tax_refund_scan_result")
    op.drop_table("sap_gl_line_observation")
    op.drop_table("income_tax_refund_target")
    op.drop_table("sap_refund_evidence_batch")
    # monitor_type labels are intentionally additive. PostgreSQL cannot safely
    # remove an enum label in place, matching the downgrade policy in 0010/0011/0019.


def _create_evidence_batch() -> None:
    op.create_table(
        "sap_refund_evidence_batch",
        _uuid_primary_key(),
        sa.Column("source_batch_key", sa.String(length=256), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("through_period", sa.Date(), nullable=False),
        sa.Column("company_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        *_audit_timestamps(),
        sa.CheckConstraint(
            "fiscal_year BETWEEN 2000 AND 9999",
            name=op.f("ck_sap_refund_evidence_batch_fiscal_year"),
        ),
        sa.CheckConstraint(
            "EXTRACT(YEAR FROM through_period) = fiscal_year",
            name=op.f("ck_sap_refund_evidence_batch_through_period_year"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(company_ids) = 'array' AND jsonb_array_length(company_ids) > 0",
            name=op.f("ck_sap_refund_evidence_batch_company_ids"),
        ),
        sa.CheckConstraint(
            "status = 'COMPLETE'",
            name=op.f("ck_sap_refund_evidence_batch_status"),
        ),
        sa.CheckConstraint(
            "record_count >= 0",
            name=op.f("ck_sap_refund_evidence_batch_nonnegative_record_count"),
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name=op.f("ck_sap_refund_evidence_batch_checksum_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sap_refund_evidence_batch")),
        sa.UniqueConstraint(
            "source_batch_key",
            name=op.f("uq_sap_refund_evidence_batch_source_key"),
        ),
    )
    op.create_index(
        "ix_sap_refund_evidence_period",
        "sap_refund_evidence_batch",
        ["fiscal_year", "through_period"],
        unique=False,
    )


def _create_refund_target() -> None:
    op.create_table(
        "income_tax_refund_target",
        _uuid_primary_key(),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refund_tax_year", sa.SmallInteger(), nullable=False),
        sa.Column("source_record_key", sa.String(length=512), nullable=False),
        sa.Column("expected_amount", sa.Numeric(38, 12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_scale", sa.SmallInteger(), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=False),
        sa.Column(
            "receipt_status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_scan_period", sa.Date(), nullable=True),
        *_audit_timestamps(),
        sa.CheckConstraint(
            "refund_tax_year BETWEEN 2000 AND 9999",
            name=op.f("ck_income_tax_refund_target_refund_tax_year"),
        ),
        sa.CheckConstraint(
            "expected_amount > 0",
            name=op.f("ck_income_tax_refund_target_positive_expected_amount"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_income_tax_refund_target_currency"),
        ),
        sa.CheckConstraint(
            "amount_scale BETWEEN 0 AND 12",
            name=op.f("ck_income_tax_refund_target_amount_scale"),
        ),
        sa.CheckConstraint(
            "btrim(source_record_key) <> ''",
            name=op.f("ck_income_tax_refund_target_source_record_key"),
        ),
        sa.CheckConstraint(
            "btrim(source_version) <> ''",
            name=op.f("ck_income_tax_refund_target_source_version"),
        ),
        sa.CheckConstraint(
            "receipt_status IN ('PENDING', 'RECEIVED')",
            name=op.f("ck_income_tax_refund_target_receipt_status"),
        ),
        sa.CheckConstraint(
            "(receipt_status = 'PENDING' AND received_at IS NULL) OR "
            "(receipt_status = 'RECEIVED' AND received_at IS NOT NULL)",
            name=op.f("ck_income_tax_refund_target_receipt_state"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name="fk_refund_target_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_income_tax_refund_target")),
        sa.UniqueConstraint(
            "company_id",
            "refund_tax_year",
            name="uq_income_tax_refund_target_company_year",
        ),
        sa.UniqueConstraint(
            "id",
            "company_id",
            name="uq_income_tax_refund_target_id_company",
        ),
    )
    op.create_index(
        op.f("ix_income_tax_refund_target_company_id"),
        "income_tax_refund_target",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_income_tax_refund_target_status",
        "income_tax_refund_target",
        ["receipt_status", "latest_scan_period"],
        unique=False,
    )


def _create_sap_gl_line() -> None:
    op.create_table(
        "sap_gl_line_observation",
        _uuid_primary_key(),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_batch_key", sa.String(length=256), nullable=False),
        sa.Column("client", sa.String(length=32), nullable=False),
        sa.Column("ledger", sa.String(length=32), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_period", sa.SmallInteger(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("document_number", sa.String(length=64), nullable=False),
        sa.Column("line_item", sa.String(length=32), nullable=False),
        sa.Column("gl_account_code", sa.String(length=64), nullable=False),
        sa.Column("gl_account_name", sa.String(length=256), nullable=False),
        sa.Column("account_category", sa.String(length=32), nullable=False),
        sa.Column("debit_credit", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(38, 12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_scale", sa.SmallInteger(), nullable=False),
        sa.Column("is_reversed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        *_audit_timestamps(),
        sa.CheckConstraint(
            "fiscal_year BETWEEN 2000 AND 9999",
            name=op.f("ck_sap_gl_line_observation_fiscal_year"),
        ),
        sa.CheckConstraint(
            "fiscal_period BETWEEN 1 AND 12",
            name=op.f("ck_sap_gl_line_observation_fiscal_period"),
        ),
        sa.CheckConstraint(
            "EXTRACT(YEAR FROM posting_date) = fiscal_year "
            "AND EXTRACT(MONTH FROM posting_date) = fiscal_period",
            name=op.f("ck_sap_gl_line_observation_posting_period"),
        ),
        sa.CheckConstraint(
            "account_category IN ('INCOME_TAX_EXPENSE', 'OTHER_INCOME')",
            name=op.f("ck_sap_gl_line_observation_account_category"),
        ),
        sa.CheckConstraint(
            "debit_credit IN ('DEBIT', 'CREDIT')",
            name=op.f("ck_sap_gl_line_observation_debit_credit"),
        ),
        sa.CheckConstraint(
            "amount >= 0",
            name=op.f("ck_sap_gl_line_observation_nonnegative_amount"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_sap_gl_line_observation_currency"),
        ),
        sa.CheckConstraint(
            "amount_scale BETWEEN 0 AND 12",
            name=op.f("ck_sap_gl_line_observation_amount_scale"),
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name=op.f("ck_sap_gl_line_observation_source_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name="fk_sap_refund_gl_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_key"],
            ["sap_refund_evidence_batch.source_batch_key"],
            name="fk_sap_refund_gl_evidence_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sap_gl_line_observation")),
        sa.UniqueConstraint(
            "source_batch_key",
            "client",
            "ledger",
            "company_id",
            "fiscal_year",
            "document_number",
            "line_item",
            name="uq_sap_gl_line_observation_business_key",
        ),
        sa.UniqueConstraint(
            "id",
            "company_id",
            name="uq_sap_gl_line_observation_id_company",
        ),
    )
    op.create_index(
        op.f("ix_sap_gl_line_observation_company_id"),
        "sap_gl_line_observation",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_sap_refund_gl_match",
        "sap_gl_line_observation",
        [
            "company_id",
            "fiscal_year",
            "fiscal_period",
            "account_category",
            "debit_credit",
            "currency",
            "amount",
        ],
        unique=False,
    )
    op.create_index(
        "ix_sap_refund_gl_source_batch",
        "sap_gl_line_observation",
        ["source_batch_key"],
        unique=False,
    )


def _create_scan_result() -> None:
    op.create_table(
        "income_tax_refund_scan_result",
        _uuid_primary_key(),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scan_period", sa.Date(), nullable=False),
        sa.Column("receipt_status", sa.String(length=32), nullable=False),
        sa.Column("account_status", sa.String(length=32), nullable=False),
        sa.Column("matched_line_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expected_amount", sa.Numeric(38, 12), nullable=False),
        sa.Column("matched_amount", sa.Numeric(38, 12), nullable=True),
        sa.Column("gl_account_code", sa.String(length=64), nullable=True),
        sa.Column("gl_account_name", sa.String(length=256), nullable=True),
        sa.Column("alert_code", sa.String(length=128), nullable=True),
        sa.Column(
            "structured_output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_audit_timestamps(),
        sa.CheckConstraint(
            "receipt_status IN ('NOT_RECEIVED', 'RECEIVED', 'AMBIGUOUS')",
            name=op.f("ck_income_tax_refund_scan_result_receipt_status"),
        ),
        sa.CheckConstraint(
            "account_status IN ('NOT_APPLICABLE', 'CORRECT', 'WRONG_ACCOUNT', 'AMBIGUOUS')",
            name=op.f("ck_income_tax_refund_scan_result_account_status"),
        ),
        sa.CheckConstraint(
            "expected_amount > 0",
            name=op.f("ck_income_tax_refund_scan_result_positive_expected_amount"),
        ),
        sa.CheckConstraint(
            "matched_amount IS NULL OR matched_amount > 0",
            name=op.f("ck_income_tax_refund_scan_result_positive_matched_amount"),
        ),
        sa.CheckConstraint(
            "(gl_account_code IS NULL AND gl_account_name IS NULL) OR "
            "(gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL)",
            name=op.f("ck_income_tax_refund_scan_result_account_detail_pair"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(structured_output) = 'object'",
            name=op.f("ck_income_tax_refund_scan_result_structured_output_object"),
        ),
        sa.CheckConstraint(
            "structured_output -> 'completeness' = 'true'::jsonb "
            "AND jsonb_typeof(structured_output -> 'source_batch_key') = 'string' "
            "AND btrim(structured_output ->> 'source_batch_key') <> ''",
            name=op.f("ck_income_tax_refund_scan_result_source_completeness"),
        ),
        sa.CheckConstraint(
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
            name=op.f("ck_income_tax_refund_scan_result_classification_state"),
        ),
        sa.ForeignKeyConstraint(
            ["target_id", "company_id"],
            ["income_tax_refund_target.id", "income_tax_refund_target.company_id"],
            name="fk_refund_scan_target_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_line_id", "company_id"],
            ["sap_gl_line_observation.id", "sap_gl_line_observation.company_id"],
            name="fk_refund_scan_matched_line_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_income_tax_refund_scan_result")),
        sa.UniqueConstraint(
            "target_id",
            "scan_period",
            name="uq_income_tax_refund_scan_target_period",
        ),
    )
    op.create_index(
        "ix_income_tax_refund_scan_company_period",
        "income_tax_refund_scan_result",
        ["company_id", "scan_period"],
        unique=False,
    )


def _create_writeback() -> None:
    op.create_table(
        "income_tax_refund_writeback",
        _uuid_primary_key(),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("desired_value", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_timestamps(),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> ''",
            name=op.f("ck_income_tax_refund_writeback_idempotency_key"),
        ),
        sa.CheckConstraint(
            "btrim(desired_value) <> ''",
            name=op.f("ck_income_tax_refund_writeback_desired_value"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_income_tax_refund_writeback_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_income_tax_refund_writeback_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND processed_at IS NULL) OR "
            "(status = 'PROCESSING' AND attempt_count > 0 AND processed_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND attempt_count > 0 AND processed_at IS NOT NULL "
            "AND last_error IS NULL) OR "
            "(status = 'FAILED' AND attempt_count > 0 AND processed_at IS NULL "
            "AND last_error IS NOT NULL AND btrim(last_error) <> '')",
            name=op.f("ck_income_tax_refund_writeback_delivery_state"),
        ),
        sa.ForeignKeyConstraint(
            ["target_id", "company_id"],
            ["income_tax_refund_target.id", "income_tax_refund_target.company_id"],
            name="fk_refund_writeback_target_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_income_tax_refund_writeback")),
        sa.UniqueConstraint(
            "target_id",
            name="uq_income_tax_refund_writeback_target",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_income_tax_refund_writeback_idempotency_key",
        ),
    )
    op.create_index(
        "ix_income_tax_refund_writeback_status",
        "income_tax_refund_writeback",
        ["status", "created_at"],
        unique=False,
    )


def _enable_rls() -> None:
    for table_name in _DIRECT_COMPANY_TABLES:
        _enable_policy(table_name, "app_company_id_allowed(company_id)")

    table_name = "sap_refund_evidence_batch"
    predicate = """
        current_setting('app.company_scope', true) = '*'
        OR (
          jsonb_array_length(company_ids) > 0
          AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(company_ids) scoped_company_id
            LEFT JOIN company scoped_company
              ON scoped_company.id::text = scoped_company_id
            WHERE scoped_company.id IS NULL
               OR NOT app_company_id_allowed(scoped_company.id)
          )
        )
    """
    _enable_policy(table_name, predicate)


def _enable_policy(table_name: str, predicate: str) -> None:
    policy_name = f"{table_name}_company_scope"
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{policy_name}" ON "{table_name}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def _uuid_primary_key() -> sa.Column[Any]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _audit_timestamps() -> tuple[sa.Column[Any], sa.Column[Any]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

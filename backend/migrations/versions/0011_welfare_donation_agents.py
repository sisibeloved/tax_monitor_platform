"""Add welfare and donation monthly semantic monitoring.

Revision ID: 0011_welfare_donation
Revises: 0010_semantic_artifacts
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011_welfare_donation"
down_revision: str | None = "0010_semantic_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE monitor_type ADD VALUE IF NOT EXISTS 'WELFARE'")
        op.execute("ALTER TYPE monitor_type ADD VALUE IF NOT EXISTS 'DONATION'")
        op.execute(
            "ALTER TYPE monitoring_run_type ADD VALUE IF NOT EXISTS 'MONTHLY_SEMANTIC'"
        )
        op.execute(
            "ALTER TYPE monitoring_run_company_status ADD VALUE IF NOT EXISTS 'NOT_RUN'"
        )

    op.drop_constraint(
        op.f("ck_sap_expense_voucher_observation_account_family"),
        "sap_expense_voucher_observation",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_sap_expense_voucher_observation_account_family"),
        "sap_expense_voucher_observation",
        "account_family IN ('BUSINESS_ENTERTAINMENT', 'WELFARE', 'DONATION')",
    )

    op.create_table(
        "semantic_version_set",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("set_key", sa.String(length=64), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prompt_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_library_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "account_dictionary_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint(
            "effective_to >= effective_from",
            name="ck_semantic_version_set_effective_period",
        ),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'RETIRED')",
            name="ck_semantic_version_set_status",
        ),
        sa.CheckConstraint(
            "length(set_key) = 64",
            name="ck_semantic_version_set_set_key_length",
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["rule_version.id"],
            name="fk_semantic_version_set_rule_version_id_rule_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_artifact_id"],
            ["semantic_artifact_version.id"],
            name="fk_sem_version_set_model_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_artifact_id"],
            ["semantic_artifact_version.id"],
            name="fk_sem_version_set_prompt_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["case_library_artifact_id"],
            ["semantic_artifact_version.id"],
            name="fk_sem_version_set_case_library_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_dictionary_version_id"],
            ["suggested_account_dictionary_version.id"],
            name="fk_sem_version_set_account_dictionary",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_semantic_version_set"),
        sa.UniqueConstraint("set_key", name="uq_semantic_version_set_key"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_semantic_version_set_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'published semantic version sets are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_semantic_version_set_immutable
        BEFORE UPDATE OR DELETE ON semantic_version_set
        FOR EACH ROW EXECUTE FUNCTION reject_semantic_version_set_mutation()
        """
    )

    op.add_column("monitoring_run", sa.Column("period", sa.Date(), nullable=True))
    op.add_column(
        "monitoring_run",
        sa.Column(
            "monitoring_type",
            postgresql.ENUM(name="monitor_type", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "monitoring_run",
        sa.Column(
            "semantic_version_set_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.alter_column("monitoring_run", "quarter", existing_type=sa.SmallInteger(), nullable=True)
    op.create_foreign_key(
        op.f("fk_monitoring_run_semantic_version_set_id_semantic_version_set"),
        "monitoring_run",
        "semantic_version_set",
        ["semantic_version_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_monitoring_run_run_contract"),
        "monitoring_run",
        "(run_type = 'QUARTERLY' AND quarter IS NOT NULL AND period IS NULL "
        "AND monitoring_type IS NULL AND semantic_version_set_id IS NULL) OR "
        "(run_type = 'MONTHLY_SEMANTIC' AND quarter IS NULL AND period IS NOT NULL "
        "AND monitoring_type IS NOT NULL AND semantic_version_set_id IS NOT NULL)",
    )

    op.add_column("monitoring_run_company", sa.Column("selected", sa.Boolean()))
    op.add_column(
        "monitoring_run_company",
        sa.Column("adjustment_amount", sa.Numeric(38, 12)),
    )
    op.add_column(
        "monitoring_run_company",
        sa.Column(
            "processed_line_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "monitoring_run_company",
        sa.Column(
            "risk_case_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "monitoring_run_company",
        sa.Column("issue_code", sa.String(length=128)),
    )
    op.create_check_constraint(
        op.f("ck_monitoring_run_company_nonnegative_monthly_counts"),
        "monitoring_run_company",
        "processed_line_count >= 0 AND risk_case_count >= 0",
    )
    op.drop_constraint(
        op.f("ck_monitoring_run_company_ck_monitoring_run_company_lif_6d53"),
        "monitoring_run_company",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_monitoring_run_company_lifecycle_state"),
        "monitoring_run_company",
        "(status = 'PENDING' AND retryable = false AND celery_task_id IS NULL "
        "AND started_at IS NULL AND finished_at IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND detection_ids = '[]'::jsonb "
        "AND case_ids = '[]'::jsonb) OR "
        "(status = 'RUNNING' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND detection_ids = '[]'::jsonb "
        "AND case_ids = '[]'::jsonb) OR "
        "(status = 'RETRY_PENDING' AND attempt_count > 0 AND retryable = true "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
        "(status = 'SUCCEEDED' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL) OR "
        "(status = 'BLOCKED' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
        "(status = 'FAILED' AND attempt_count > 0 AND retryable = true "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
        "(status = 'NOT_RUN' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND issue_code IS NOT NULL AND btrim(issue_code) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb)",
    )

    op.add_column(
        "semantic_detection_record",
        sa.Column(
            "monitoring_type",
            postgresql.ENUM(name="monitor_type", create_type=False),
            server_default=sa.text("'BUSINESS_ENTERTAINMENT'::monitor_type"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("semantic_detection_record", "monitoring_type")
    op.drop_constraint(
        op.f("ck_monitoring_run_company_lifecycle_state"),
        "monitoring_run_company",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_monitoring_run_company_ck_monitoring_run_company_lif_6d53"),
        "monitoring_run_company",
        "(status = 'PENDING' AND retryable = false AND celery_task_id IS NULL "
        "AND started_at IS NULL AND finished_at IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND detection_ids = '[]'::jsonb "
        "AND case_ids = '[]'::jsonb) OR "
        "(status = 'RUNNING' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND detection_ids = '[]'::jsonb "
        "AND case_ids = '[]'::jsonb) OR "
        "(status = 'RETRY_PENDING' AND attempt_count > 0 AND retryable = true "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
        "(status = 'SUCCEEDED' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL) OR "
        "(status = 'BLOCKED' AND attempt_count > 0 AND retryable = false "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
        "(status = 'FAILED' AND attempt_count > 0 AND retryable = true "
        "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
        "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
        "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb)",
    )
    op.drop_constraint(
        op.f("ck_monitoring_run_company_nonnegative_monthly_counts"),
        "monitoring_run_company",
        type_="check",
    )
    for column in (
        "issue_code",
        "risk_case_count",
        "processed_line_count",
        "adjustment_amount",
        "selected",
    ):
        op.drop_column("monitoring_run_company", column)
    op.drop_constraint(
        op.f("ck_monitoring_run_run_contract"), "monitoring_run", type_="check"
    )
    op.drop_constraint(
        op.f("fk_monitoring_run_semantic_version_set_id_semantic_version_set"),
        "monitoring_run",
        type_="foreignkey",
    )
    op.drop_column("monitoring_run", "semantic_version_set_id")
    op.drop_column("monitoring_run", "monitoring_type")
    op.drop_column("monitoring_run", "period")
    op.alter_column("monitoring_run", "quarter", existing_type=sa.SmallInteger(), nullable=False)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_semantic_version_set_immutable ON semantic_version_set"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_semantic_version_set_mutation()")
    op.drop_table("semantic_version_set")
    op.drop_constraint(
        op.f("ck_sap_expense_voucher_observation_account_family"),
        "sap_expense_voucher_observation",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_sap_expense_voucher_observation_account_family"),
        "sap_expense_voucher_observation",
        "account_family = 'BUSINESS_ENTERTAINMENT'",
    )

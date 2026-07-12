"""Add durable per-company state for quarterly batch orchestration.

Revision ID: 0005_quarterly_batch_state
Revises: 0004_quarterly_detection
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_quarterly_batch_state"
down_revision: str | None = "0004_quarterly_detection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COMPANY_STATUSES: tuple[str, ...] = (
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "BLOCKED",
    "FAILED",
)


def upgrade() -> None:
    status_type = postgresql.ENUM(
        *COMPANY_STATUSES,
        name="monitoring_run_company_status",
    )
    status_type.create(op.get_bind(), checkfirst=False)
    status_column_type = postgresql.ENUM(
        *COMPANY_STATUSES,
        name="monitoring_run_company_status",
        create_type=False,
    )

    op.create_table(
        "monitoring_run_company",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "snapshot_set_member_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            status_column_type,
            server_default=sa.text("'PENDING'::monitoring_run_company_status"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "retryable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "detection_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "case_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
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
            "attempt_count >= 0",
            name="ck_monitoring_run_company_nonnegative_attempt_count",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(detection_ids) = 'array' AND jsonb_typeof(case_ids) = 'array'",
            name="ck_monitoring_run_company_result_ids_are_arrays",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name="ck_monitoring_run_company_attempt_time_order",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND retryable = false "
            "AND celery_task_id IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND error_code IS NULL AND error_message IS NULL "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'RUNNING' AND attempt_count > 0 AND retryable = false "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_code IS NULL AND error_message IS NULL "
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
            name="ck_monitoring_run_company_lifecycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["monitoring_run.id"],
            name="fk_run_company_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_set_member_id"],
            ["snapshot_set_member.id"],
            name="fk_run_company_member",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitoring_run_company"),
        sa.UniqueConstraint(
            "run_id",
            "snapshot_set_member_id",
            name="uq_monitoring_run_company_run_member",
        ),
    )
    op.create_index(
        "ix_monitoring_run_company_run_status",
        "monitoring_run_company",
        ["run_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_monitoring_run_company_snapshot_set_member_id",
        "monitoring_run_company",
        ["snapshot_set_member_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monitoring_run_company_snapshot_set_member_id",
        table_name="monitoring_run_company",
    )
    op.drop_index(
        "ix_monitoring_run_company_run_status",
        table_name="monitoring_run_company",
    )
    op.drop_table("monitoring_run_company")
    postgresql.ENUM(
        *COMPANY_STATUSES,
        name="monitoring_run_company_status",
    ).drop(op.get_bind(), checkfirst=False)

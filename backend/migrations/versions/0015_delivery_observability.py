"""Add durable batch and company delivery timestamps.

Revision ID: 0015_delivery_observability
Revises: 0014_export_jobs
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_delivery_observability"
down_revision: str | None = "0014_export_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitoring_run",
        sa.Column("batch_finished_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "monitoring_run",
        sa.Column("output_ready_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "monitoring_run_company",
        sa.Column("company_output_ready_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        """
        UPDATE monitoring_run_company
        SET company_output_ready_at = finished_at
        WHERE status = 'SUCCEEDED' AND finished_at IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE monitoring_run
        SET batch_finished_at = finished_at
        WHERE status IN ('PARTIAL_SUCCESS', 'SUCCEEDED', 'FAILED')
          AND finished_at IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE monitoring_run AS run
        SET output_ready_at = outputs.latest_ready_at
        FROM (
          SELECT run_id, max(company_output_ready_at) AS latest_ready_at,
                 count(*) AS company_count,
                 count(company_output_ready_at) AS ready_count
          FROM monitoring_run_company
          GROUP BY run_id
        ) AS outputs
        WHERE run.id = outputs.run_id
          AND run.status = 'SUCCEEDED'
          AND outputs.company_count = outputs.ready_count
        """
    )
    op.create_check_constraint(
        "ck_monitoring_run_batch_finished_terminal",
        "monitoring_run",
        "batch_finished_at IS NULL OR status IN ('PARTIAL_SUCCESS', 'SUCCEEDED', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_monitoring_run_output_ready_success",
        "monitoring_run",
        "output_ready_at IS NULL OR status = 'SUCCEEDED'",
    )
    op.create_check_constraint(
        "ck_monitoring_run_company_output_ready_success",
        "monitoring_run_company",
        "company_output_ready_at IS NULL OR status = 'SUCCEEDED'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_monitoring_run_company_output_ready_success",
        "monitoring_run_company",
        type_="check",
    )
    op.drop_constraint(
        "ck_monitoring_run_output_ready_success",
        "monitoring_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_monitoring_run_batch_finished_terminal",
        "monitoring_run",
        type_="check",
    )
    op.drop_column("monitoring_run_company", "company_output_ready_at")
    op.drop_column("monitoring_run", "output_ready_at")
    op.drop_column("monitoring_run", "batch_finished_at")

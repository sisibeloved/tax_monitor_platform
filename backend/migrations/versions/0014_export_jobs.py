"""Add scoped asynchronous export jobs.

Revision ID: 0014_export_jobs
Revises: 0013_audit_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0014_export_jobs"
down_revision: str | None = "0013_audit_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_job",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("export_type", sa.String(64), nullable=False),
        sa.Column("requester_subject", sa.String(256), nullable=False),
        sa.Column("requester_roles", postgresql.JSONB(), nullable=False),
        sa.Column("company_ids", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_filters", postgresql.JSONB(), nullable=False),
        sa.Column("filters_hash", sa.String(64), nullable=False),
        sa.Column("authorization_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("row_count", sa.Integer()),
        sa.Column("checksum", sa.String(64)),
        sa.Column("object_key", sa.Text()),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
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
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'EXPIRED')",
            name=op.f("ck_export_job_status"),
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count >= 0",
            name=op.f("ck_export_job_nonnegative_rows"),
        ),
        sa.CheckConstraint(
            "checksum IS NULL OR length(checksum) = 64",
            name=op.f("ck_export_job_checksum_length"),
        ),
        sa.CheckConstraint(
            "length(filters_hash) = 64",
            name=op.f("ck_export_job_filters_hash_length"),
        ),
        sa.CheckConstraint(
            "length(authorization_version) = 64",
            name=op.f("ck_export_job_authorization_version_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_job")),
    )
    op.create_index(
        "ix_export_job_requester_status",
        "export_job",
        ["requester_subject", "status"],
    )
    op.create_index("ix_export_job_expires_at", "export_job", ["expires_at"])
    op.execute("ALTER TABLE export_job ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE export_job FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY export_job_company_scope ON export_job
        USING (
          current_setting('app.company_scope', true) = '*'
          OR EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(company_ids) scoped_company
            WHERE app_company_id_allowed(scoped_company::uuid)
          )
        )
        WITH CHECK (
          current_setting('app.company_scope', true) = '*'
          OR EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(company_ids) scoped_company
            WHERE app_company_id_allowed(scoped_company::uuid)
          )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS export_job_company_scope ON export_job")
    op.drop_index("ix_export_job_expires_at", table_name="export_job")
    op.drop_index("ix_export_job_requester_status", table_name="export_job")
    op.drop_table("export_job")


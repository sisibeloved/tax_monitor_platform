"""Add governed business-entertainment company scope versions.

Revision ID: 0007_entertainment_scope
Revises: 0006_review_action_assignee
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_entertainment_scope"
down_revision: str | None = "0006_review_action_assignee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCOPE_STATUSES = ("DRAFT", "APPROVED", "PUBLISHED", "RETIRED")


def upgrade() -> None:
    status_type = postgresql.ENUM(
        *SCOPE_STATUSES,
        name="business_entertainment_scope_status",
    )
    status_type.create(op.get_bind(), checkfirst=False)
    status_column_type = postgresql.ENUM(
        *SCOPE_STATUSES,
        name="business_entertainment_scope_status",
        create_type=False,
    )

    op.create_table(
        "business_entertainment_scope_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=False),
        sa.Column("source_file_name", sa.Text(), nullable=False),
        sa.Column("file_checksum", sa.String(length=64), nullable=False),
        sa.Column("uploader_id", sa.String(length=256), nullable=False),
        sa.Column("reviewer_id", sa.String(length=256), nullable=True),
        sa.Column(
            "status",
            status_column_type,
            server_default=sa.text("'DRAFT'::business_entertainment_scope_status"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(length=256), nullable=True),
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
            name="ck_business_entertainment_scope_version_effective_period",
        ),
        sa.CheckConstraint(
            "length(file_checksum) = 64",
            name="ck_business_entertainment_scope_version_file_checksum_length",
        ),
        sa.CheckConstraint(
            "(status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status = 'APPROVED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status IN ('PUBLISHED', 'RETIRED') AND reviewer_id IS NOT NULL "
            "AND approved_at IS NOT NULL AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL)",
            name="ck_business_entertainment_scope_version_status_audit",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["ingest_batch.id"],
            name="fk_be_scope_version_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_entertainment_scope_version"),
        sa.UniqueConstraint(
            "batch_id",
            name="uq_business_entertainment_scope_version_batch_id",
        ),
    )

    op.create_table(
        "business_entertainment_scope_company",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name="fk_be_scope_company_company",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["source_record.id"],
            name="fk_be_scope_company_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["business_entertainment_scope_version.id"],
            name="fk_be_scope_company_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_entertainment_scope_company"),
        sa.UniqueConstraint(
            "version_id",
            "company_id",
            name="uq_be_scope_version_company",
        ),
        sa.UniqueConstraint(
            "version_id",
            "source_record_id",
            name="uq_be_scope_version_source",
        ),
        sa.UniqueConstraint(
            "source_record_id",
            name="uq_business_entertainment_scope_company_source_record_id",
        ),
    )
    op.create_index(
        op.f("ix_business_entertainment_scope_company_company_id"),
        "business_entertainment_scope_company",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_business_entertainment_scope_company_version_id"),
        "business_entertainment_scope_company",
        ["version_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION protect_published_business_entertainment_scope_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
                RAISE EXCEPTION 'immutable_business_entertainment_scope';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_published_business_entertainment_scope_version
        BEFORE UPDATE OR DELETE ON business_entertainment_scope_version
        FOR EACH ROW
        EXECUTE FUNCTION protect_published_business_entertainment_scope_version()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_published_business_entertainment_scope_company()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM business_entertainment_scope_version
                WHERE id = OLD.version_id
                  AND status IN ('PUBLISHED', 'RETIRED')
            ) THEN
                RAISE EXCEPTION 'immutable_business_entertainment_scope';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_published_business_entertainment_scope_company
        BEFORE UPDATE OR DELETE ON business_entertainment_scope_company
        FOR EACH ROW
        EXECUTE FUNCTION protect_published_business_entertainment_scope_company()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_protect_published_business_entertainment_scope_company "
        "ON business_entertainment_scope_company"
    )
    op.execute("DROP FUNCTION protect_published_business_entertainment_scope_company()")
    op.execute(
        "DROP TRIGGER trg_protect_published_business_entertainment_scope_version "
        "ON business_entertainment_scope_version"
    )
    op.execute("DROP FUNCTION protect_published_business_entertainment_scope_version()")
    op.drop_index(
        op.f("ix_business_entertainment_scope_company_version_id"),
        table_name="business_entertainment_scope_company",
    )
    op.drop_index(
        op.f("ix_business_entertainment_scope_company_company_id"),
        table_name="business_entertainment_scope_company",
    )
    op.drop_table("business_entertainment_scope_company")
    op.drop_table("business_entertainment_scope_version")
    postgresql.ENUM(
        *SCOPE_STATUSES,
        name="business_entertainment_scope_status",
    ).drop(op.get_bind(), checkfirst=False)

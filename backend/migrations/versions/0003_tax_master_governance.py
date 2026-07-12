"""Add explicit tax-master import governance audit fields.

Revision ID: 0003_tax_master_governance
Revises: 0002_company_master_freshness
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_tax_master_governance"
down_revision: str | None = "0002_company_master_freshness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_master_version",
        sa.Column(
            "source_row_number",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "tax_master_version",
        sa.Column(
            "uploaded_by",
            sa.String(length=256),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE tax_master_version
        SET source_row_number = 2,
            uploaded_by = 'legacy-migration'
        WHERE source_row_number IS NULL OR uploaded_by IS NULL
        """
    )
    op.alter_column(
        "tax_master_version",
        "source_row_number",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "tax_master_version",
        "uploaded_by",
        existing_type=sa.String(length=256),
        nullable=False,
    )
    op.execute(
        """
        UPDATE tax_master_version
        SET approved_by = 'legacy-migration'
        WHERE status IN ('PUBLISHED', 'RETIRED')
          AND (approved_by IS NULL OR btrim(approved_by) = '')
        """
    )
    op.execute(
        """
        UPDATE tax_master_version
        SET approved_by = NULL
        WHERE status = 'DRAFT'
        """
    )
    op.drop_constraint(
        op.f("ck_tax_master_published_at_state"),
        "tax_master_version",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_tax_master_version_published_at_state"),
        "tax_master_version",
        "(status = 'DRAFT' AND published_at IS NULL AND approved_by IS NULL) OR "
        "(status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL "
        "AND approved_by IS NOT NULL AND btrim(approved_by) <> '')",
    )
    op.create_check_constraint(
        op.f("ck_tax_master_version_nonnegative_loss_carryforward"),
        "tax_master_version",
        "loss_carryforward >= 0",
    )
    op.create_check_constraint(
        op.f("ck_tax_master_version_positive_source_row_number"),
        "tax_master_version",
        "source_row_number > 1",
    )
    op.create_check_constraint(
        op.f("ck_tax_master_version_source_checksum_length"),
        "tax_master_version",
        "source_checksum IS NULL OR length(source_checksum) = 64",
    )
    op.create_index(
        op.f("ix_tax_master_version_source_batch_id"),
        "tax_master_version",
        ["source_batch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_tax_master_version_source_batch_id"),
        table_name="tax_master_version",
    )
    op.drop_constraint(
        op.f("ck_tax_master_version_source_checksum_length"),
        "tax_master_version",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_tax_master_version_positive_source_row_number"),
        "tax_master_version",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_tax_master_version_nonnegative_loss_carryforward"),
        "tax_master_version",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_tax_master_version_published_at_state"),
        "tax_master_version",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_tax_master_published_at_state"),
        "tax_master_version",
        "(status = 'DRAFT' AND published_at IS NULL) OR "
        "(status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL)",
    )
    op.drop_column("tax_master_version", "uploaded_by")
    op.drop_column("tax_master_version", "source_row_number")

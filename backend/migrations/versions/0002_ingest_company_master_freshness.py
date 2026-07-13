"""Track the freshest applied company-master event.

Revision ID: 0002_company_master_freshness
Revises: 0001_control_plane
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_company_master_freshness"
down_revision: str | None = "0001_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "company",
        sa.Column(
            "master_data_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE company
        SET master_data_updated_at = COALESCE(
            lifecycle_changed_at,
            updated_at,
            created_at,
            CURRENT_TIMESTAMP
        )
        WHERE master_data_updated_at IS NULL
        """
    )
    op.alter_column("company", "master_data_updated_at", nullable=False)
    op.alter_column(
        "company",
        "master_data_updated_at",
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def downgrade() -> None:
    op.drop_column("company", "master_data_updated_at")

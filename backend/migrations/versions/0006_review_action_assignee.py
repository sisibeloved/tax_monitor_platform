"""Preserve assignment ownership in the immutable review audit trail.

Revision ID: 0006_review_action_assignee
Revises: 0005_quarterly_batch_state
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_review_action_assignee"
down_revision: str | None = "0005_quarterly_batch_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_action",
        sa.Column("assignee", sa.String(length=256), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_review_action_assignee_action"),
        "review_action",
        "assignee IS NULL OR (action = 'ASSIGN' AND btrim(assignee) <> '')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_review_action_assignee_action"),
        "review_action",
        type_="check",
    )
    op.drop_column("review_action", "assignee")

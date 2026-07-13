"""Add auditable SAP identity and amount fields to link coverage.

Revision ID: 0008b_ent_coverage_fields
Revises: 0008a_ent_snapshot_guard
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008b_ent_coverage_fields"
down_revision: str | None = "0008a_ent_snapshot_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sap_link_coverage", sa.Column("document_number", sa.String(64)))
    op.add_column("sap_link_coverage", sa.Column("line_item", sa.String(32)))
    op.add_column("sap_link_coverage", sa.Column("amount", sa.Numeric(38, 12)))
    op.add_column("sap_link_coverage", sa.Column("currency", sa.String(3)))
    op.execute(
        """
        UPDATE sap_link_coverage AS coverage
        SET document_number = observation.document_number,
            line_item = observation.line_item,
            amount = observation.amount,
            currency = observation.currency
        FROM sap_expense_voucher_observation AS observation
        WHERE observation.id = coverage.sap_observation_id
        """
    )
    for column_name in ("document_number", "line_item", "amount", "currency"):
        op.alter_column("sap_link_coverage", column_name, nullable=False)
    op.create_check_constraint(
        op.f("ck_sap_link_coverage_currency"),
        "sap_link_coverage",
        "currency ~ '^[A-Z]{3}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_sap_link_coverage_currency"),
        "sap_link_coverage",
        type_="check",
    )
    for column_name in ("currency", "amount", "line_item", "document_number"):
        op.drop_column("sap_link_coverage", column_name)

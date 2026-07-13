"""Prevent SAP observations from being attached after snapshot-set publication.

Revision ID: 0008a_ent_snapshot_guard
Revises: 0008_entertainment_observations
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0008a_ent_snapshot_guard"
down_revision: str | None = "0008_entertainment_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_late_sap_snapshot_projection()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM snapshot_set_member AS member
                JOIN snapshot_set AS snapshot_group
                  ON snapshot_group.id = member.snapshot_set_id
                WHERE member.snapshot_id = NEW.snapshot_id
                  AND snapshot_group.status = 'PUBLISHED'
            ) THEN
                RAISE EXCEPTION 'immutable_snapshot: cannot attach SAP observation after publication'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_sap_projection_no_late_insert "
        "BEFORE INSERT ON sap_expense_voucher_snapshot_projection FOR EACH ROW "
        "EXECUTE FUNCTION reject_late_sap_snapshot_projection()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_sap_projection_no_late_insert "
        "ON sap_expense_voucher_snapshot_projection"
    )
    op.execute("DROP FUNCTION reject_late_sap_snapshot_projection()")

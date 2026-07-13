"""Harden the shared audit event as an immutable structured ledger.

Revision ID: 0013_audit_hardening
Revises: 0012_company_isolation
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0013_audit_hardening"
down_revision: str | None = "0012_company_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_event",
        sa.Column(
            "actor_roles",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_event",
        sa.Column(
            "company_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("audit_event", sa.Column("request_id", sa.String(128)))
    op.add_column("audit_event", sa.Column("batch_id", postgresql.UUID(as_uuid=True)))
    op.add_column("audit_event", sa.Column("query_id", postgresql.UUID(as_uuid=True)))
    op.add_column("audit_event", sa.Column("export_job_id", postgresql.UUID(as_uuid=True)))
    op.add_column("audit_event", sa.Column("filters_hash", sa.String(64)))
    op.add_column("audit_event", sa.Column("row_count", sa.Integer()))
    op.add_column(
        "audit_event",
        sa.Column(
            "before_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_event",
        sa.Column(
            "after_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_event",
        sa.Column(
            "result",
            sa.String(32),
            server_default=sa.text("'SUCCEEDED'"),
            nullable=False,
        ),
    )
    op.add_column("audit_event", sa.Column("reason_code", sa.String(128)))
    op.create_check_constraint(
        op.f("ck_audit_event_filters_hash_length"),
        "audit_event",
        "filters_hash IS NULL OR length(filters_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_audit_event_nonnegative_row_count"),
        "audit_event",
        "row_count IS NULL OR row_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_audit_event_result"),
        "audit_event",
        "result IN ('SUCCEEDED', 'DENIED', 'FAILED')",
    )
    op.create_index("ix_audit_event_actor_time", "audit_event", ["actor", "occurred_at"])
    op.create_index("ix_audit_event_action_time", "audit_event", ["action", "occurred_at"])
    op.create_index(op.f("ix_audit_event_request_id"), "audit_event", ["request_id"])
    op.create_index(op.f("ix_audit_event_batch_id"), "audit_event", ["batch_id"])

    op.execute(
        """
        CREATE FUNCTION normalize_audit_event_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          context_actor text;
        BEGIN
          context_actor := current_setting('app.subject', true);
          IF context_actor IS NOT NULL AND btrim(context_actor) <> '' THEN
            NEW.actor := context_actor;
          END IF;
          NEW.occurred_at := clock_timestamp();
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'audit events are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_normalize
        BEFORE INSERT ON audit_event
        FOR EACH ROW EXECUTE FUNCTION normalize_audit_event_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_immutable
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
        """
    )
    op.execute("ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_event FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_event_company_scope ON audit_event
        USING (
          current_setting('app.company_scope', true) = '*'
          OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(company_ids) scoped_company
            WHERE app_company_id_allowed(scoped_company::uuid)
          )
        )
        WITH CHECK (
          current_setting('app.company_scope', true) = '*'
          OR (actor = 'anonymous' AND result = 'DENIED' AND company_ids = '[]'::jsonb)
          OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(company_ids) scoped_company
            WHERE app_company_id_allowed(scoped_company::uuid)
          )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_event_company_scope ON audit_event")
    op.execute("ALTER TABLE audit_event NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_event DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_immutable ON audit_event")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_normalize ON audit_event")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
    op.execute("DROP FUNCTION normalize_audit_event_insert()")
    op.drop_index(op.f("ix_audit_event_batch_id"), table_name="audit_event")
    op.drop_index(op.f("ix_audit_event_request_id"), table_name="audit_event")
    op.drop_index("ix_audit_event_action_time", table_name="audit_event")
    op.drop_index("ix_audit_event_actor_time", table_name="audit_event")
    op.drop_constraint(op.f("ck_audit_event_result"), "audit_event", type_="check")
    op.drop_constraint(
        op.f("ck_audit_event_nonnegative_row_count"), "audit_event", type_="check"
    )
    op.drop_constraint(
        op.f("ck_audit_event_filters_hash_length"), "audit_event", type_="check"
    )
    for column_name in (
        "reason_code",
        "result",
        "after_summary",
        "before_summary",
        "row_count",
        "filters_hash",
        "export_job_id",
        "query_id",
        "batch_id",
        "request_id",
        "company_ids",
        "actor_roles",
    ):
        op.drop_column("audit_event", column_name)

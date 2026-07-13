"""Close runtime RLS gaps for shared scopes and company-owned child records.

Revision ID: 0017_strict_rls_runtime
Revises: 0016_release_manifests
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0017_strict_rls_runtime"
down_revision: str | None = "0016_release_manifests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GROUP_ONLY_TABLES = (
    "ingest_batch",
    "ingest_error",
)

_CHILD_SCOPE_POLICIES = {
    "snapshot_source": """
        EXISTS (
          SELECT 1
          FROM accounting_snapshot scoped_snapshot
          WHERE scoped_snapshot.id = snapshot_source.snapshot_id
            AND app_company_id_allowed(scoped_snapshot.company_id)
        )
    """,
    "monitoring_run_company": """
        EXISTS (
          SELECT 1
          FROM snapshot_set_member scoped_member
          WHERE scoped_member.id = monitoring_run_company.snapshot_set_member_id
            AND app_company_id_allowed(scoped_member.company_id)
        )
    """,
    "review_action": """
        EXISTS (
          SELECT 1
          FROM risk_case scoped_case
          WHERE scoped_case.id = review_action.risk_case_id
            AND app_company_id_allowed(scoped_case.company_id)
        )
    """,
    "business_entertainment_case_detail": """
        EXISTS (
          SELECT 1
          FROM risk_case scoped_case
          WHERE scoped_case.id = business_entertainment_case_detail.risk_case_id
            AND app_company_id_allowed(scoped_case.company_id)
        )
    """,
}


def upgrade() -> None:
    op.execute("DROP POLICY audit_event_company_scope ON audit_event")
    op.execute(
        """
        CREATE POLICY audit_event_company_scope ON audit_event
        USING (
          current_setting('app.company_scope', true) = '*'
          OR (
            result = 'DENIED'
            AND company_ids = '[]'::jsonb
            AND actor = current_setting('app.subject', true)
          )
          OR (
            jsonb_array_length(company_ids) > 0
            AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(company_ids) scoped_company
              WHERE NOT app_company_id_allowed(scoped_company::uuid)
            )
          )
        )
        WITH CHECK (
          current_setting('app.company_scope', true) = '*'
          OR (result = 'DENIED' AND company_ids = '[]'::jsonb)
          OR (
            jsonb_array_length(company_ids) > 0
            AND NOT EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(company_ids) scoped_company
              WHERE NOT app_company_id_allowed(scoped_company::uuid)
            )
          )
        )
        """
    )
    op.execute("DROP POLICY export_job_company_scope ON export_job")
    op.execute(
        """
        CREATE POLICY export_job_company_scope ON export_job
        USING (
          current_setting('app.company_scope', true) = '*'
          OR (
            jsonb_array_length(company_ids) > 0
            AND NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(company_ids) scoped_company
              WHERE NOT app_company_id_allowed(scoped_company::uuid)
            )
          )
        )
        WITH CHECK (
          current_setting('app.company_scope', true) = '*'
          OR (
            jsonb_array_length(company_ids) > 0
            AND NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(company_ids) scoped_company
              WHERE NOT app_company_id_allowed(scoped_company::uuid)
            )
          )
        )
        """
    )
    for table_name in _GROUP_ONLY_TABLES:
        _enable_policy(
            table_name,
            "current_setting('app.company_scope', true) = '*'",
        )
    for table_name, predicate in _CHILD_SCOPE_POLICIES.items():
        _enable_policy(table_name, predicate)


def downgrade() -> None:
    for table_name in (*_CHILD_SCOPE_POLICIES, *_GROUP_ONLY_TABLES):
        policy_name = _policy_name(table_name)
        op.execute(f'DROP POLICY IF EXISTS "{policy_name}" ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP POLICY export_job_company_scope ON export_job")
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
    op.execute("DROP POLICY audit_event_company_scope ON audit_event")
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


def _enable_policy(table_name: str, predicate: str) -> None:
    policy_name = _policy_name(table_name)
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{policy_name}" ON "{table_name}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def _policy_name(table_name: str) -> str:
    return f"{table_name}_company_scope"

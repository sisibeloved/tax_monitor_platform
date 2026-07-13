"""Enforce company scope with PostgreSQL row-level security.

Revision ID: 0012_company_isolation
Revises: 0011_welfare_donation
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0012_company_isolation"
down_revision: str | None = "0011_welfare_donation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DIRECT_COMPANY_TABLES = {
    "company": "id",
    "accounting_snapshot": "company_id",
    "business_entertainment_scope_company": "company_id",
    "detection_record": "company_id",
    "risk_case": "company_id",
    "snapshot_set_member": "company_id",
    "source_record": "company_id",
    "tax_master_version": "company_id",
}

_COMPANY_CODE_TABLES = (
    "business_entertainment_evaluation",
    "business_entertainment_source_observation",
    "evidence_link",
    "sap_expense_voucher_observation",
    "sap_expense_voucher_snapshot_projection",
    "sap_link_coverage",
    "semantic_detection_record",
    "semantic_evidence_task",
    "semantic_model_call_audit",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app_company_id_allowed(candidate uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
          SELECT candidate IS NOT NULL AND (
            current_setting('app.company_scope', true) = '*'
            OR candidate::text = ANY(
              string_to_array(
                coalesce(current_setting('app.company_scope', true), ''),
                ','
              )
            )
          )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_company_code_allowed(candidate text)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
          SELECT EXISTS (
            SELECT 1
            FROM company scoped_company
            WHERE scoped_company.company_code = candidate
              AND app_company_id_allowed(scoped_company.id)
          )
        $$
        """
    )

    for table_name, column_name in _DIRECT_COMPANY_TABLES.items():
        _enable_policy(
            table_name,
            f"app_company_id_allowed({column_name})",
        )
    for table_name in _COMPANY_CODE_TABLES:
        _enable_policy(table_name, "app_company_code_allowed(company_code)")


def downgrade() -> None:
    for table_name in (*_COMPANY_CODE_TABLES, *_DIRECT_COMPANY_TABLES):
        policy_name = _policy_name(table_name)
        op.execute(f'DROP POLICY IF EXISTS "{policy_name}" ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP FUNCTION app_company_code_allowed(text)")
    op.execute("DROP FUNCTION app_company_id_allowed(uuid)")


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


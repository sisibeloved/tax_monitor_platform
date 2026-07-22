"""Add deferred income tax accrual and reversal accuracy monitoring.

Revision ID: 0019_deferred_tax_accuracy
Revises: 0018_nonpositive_revenue_tax_burden
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
import json

from alembic import op
import sqlalchemy as sa


revision: str = "0019_deferred_tax_accuracy"
down_revision: str | None = "0018_nonpositive_revenue_tax_burden"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RULE_CODE = "QUARTERLY_V2"
RULE_VERSION = "deferred-tax-reviewed"
RULE_CHANGE_REASON = (
    "Fixed, reviewed quarterly formula manifest with deferred income tax accuracy."
)
RULE_APPROVER = "deferred-tax-business-rule-confirmation-2026-07-16"
MIGRATION_PROVENANCE: dict[str, str] = {
    "revision": revision,
    "seed": f"{RULE_CODE}:{RULE_VERSION}",
}
FORMULA_MANIFEST: dict[str, object] = {
    "schema_version": RULE_CODE,
    "rounding_mode": "ROUND_HALF_UP",
    "formulas": {
        "base_before_floor": (
            "cumulative_profit-received_dividends-fair_value_change-loss_carryforward"
        ),
        "cumulative_base": "max(base_before_floor,0)",
        "cumulative_tax_payable": "cumulative_base*tax_rate",
        "current_quarter_should_accrue": (
            "cumulative_tax_payable-prior_quarter_current_tax"
        ),
        "current_quarter_difference": (
            "current_quarter_should_accrue-current_quarter_current_tax"
        ),
        "current_tax_burden": (
            "0 if cumulative_revenue<=0 else cumulative_tax_payable/cumulative_revenue"
        ),
        "tax_burden_deviation": (
            "current_tax_burden-historical_average_tax_burden"
        ),
        "potential_adjustment": "other_payables_accrual+hesi_no_invoice",
        "potential_base": "max(base_before_floor+potential_adjustment,0)",
        "potential_tax_payable": "potential_base*tax_rate",
        "potential_tax_cost": "potential_tax_payable-cumulative_tax_payable",
        "deferred_tax_base": "loss_carryforward+cumulative_profit",
        "system_cumulative_deferred_tax": "deferred_tax_base*deferred_tax_rate",
        "current_year_deferred_tax_adjustment": (
            "system_cumulative_deferred_tax-sap_cumulative_deferred_tax_expense"
        ),
    },
    "alert_boundaries": {
        "accrual_accuracy": "difference != 0",
        "tax_burden": "deviation >= 0.05 or deviation <= -0.05",
        "potential_tax_cost": "cost != 0",
        "deferred_tax_accuracy": "adjustment != 0",
    },
}


def _manifest_definition() -> dict[str, object]:
    canonical = json.dumps(
        FORMULA_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "review_status": "REVIEWED",
        "formula_manifest": FORMULA_MANIFEST,
        "formula_manifest_sha256": sha256(canonical).hexdigest(),
        "migration_provenance": MIGRATION_PROVENANCE,
    }


def _existing_rule() -> dict[str, object] | None:
    row = op.get_bind().execute(
        sa.text(
            "SELECT id, status, effective_from, effective_to, definition, "
            "change_reason, published_at, approved_by FROM rule_version "
            "WHERE rule_code = :rule_code AND version = :version"
        ),
        {"rule_code": RULE_CODE, "version": RULE_VERSION},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _is_owned_rule(row: Mapping[str, object]) -> bool:
    return (
        row["status"] == "PUBLISHED"
        and row["effective_from"] == date(2000, 1, 1)
        and row["effective_to"] is None
        and row["definition"] == _manifest_definition()
        and row["change_reason"] == RULE_CHANGE_REASON
        and row["published_at"] is not None
        and row["approved_by"] == RULE_APPROVER
    )


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "LOCK TABLE tax_master_version, rule_version, monitoring_run, "
            "detection_record, risk_case, semantic_detection_record "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    referenced_rule = bind.execute(
        sa.text(
            "SELECT monitoring_run.id FROM monitoring_run "
            "JOIN rule_version ON rule_version.id = monitoring_run.rule_version_id "
            "WHERE rule_version.rule_code = :rule_code "
            "AND rule_version.version = :version LIMIT 1"
        ),
        {"rule_code": RULE_CODE, "version": RULE_VERSION},
    ).scalar_one_or_none()
    if referenced_rule is not None:
        raise RuntimeError(
            "DEFERRED_TAX_DOWNGRADE_BLOCKED: QUARTERLY_V2 is referenced by "
            f"monitoring run {referenced_rule}"
        )

    deferred_detection = bind.execute(
        sa.text(
            "SELECT source_table, row_id FROM ("
            "SELECT 'detection_record' AS source_table, id AS row_id "
            "FROM detection_record WHERE monitor_type = 'DEFERRED_TAX_ACCURACY' "
            "UNION ALL "
            "SELECT 'risk_case' AS source_table, id AS row_id "
            "FROM risk_case WHERE monitor_type = 'DEFERRED_TAX_ACCURACY' "
            "UNION ALL "
            "SELECT 'monitoring_run' AS source_table, id AS row_id "
            "FROM monitoring_run WHERE monitoring_type = 'DEFERRED_TAX_ACCURACY' "
            "UNION ALL "
            "SELECT 'semantic_detection_record' AS source_table, id AS row_id "
            "FROM semantic_detection_record "
            "WHERE monitoring_type = 'DEFERRED_TAX_ACCURACY'"
            ") AS deferred_rows LIMIT 1"
        )
    ).mappings().one_or_none()
    if deferred_detection is not None:
        raise RuntimeError(
            "DEFERRED_TAX_DOWNGRADE_BLOCKED: deferred-tax monitoring data exists in "
            f"{deferred_detection['source_table']} ({deferred_detection['row_id']})"
        )

    populated_rate = bind.execute(
        sa.text(
            "SELECT id FROM tax_master_version "
            "WHERE deferred_tax_rate IS NOT NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    if populated_rate is not None:
        raise RuntimeError(
            "DEFERRED_TAX_DOWNGRADE_BLOCKED: tax master deferred-tax rate exists "
            f"({populated_rate})"
        )


def upgrade() -> None:
    existing_rule = _existing_rule()
    if existing_rule is not None and not _is_owned_rule(existing_rule):
        raise RuntimeError(
            "refusing to overwrite preexisting fixed quarterly rule "
            f"{RULE_CODE}:{RULE_VERSION} ({existing_rule['id']})"
        )

    # PostgreSQL requires a commit before a newly added enum value can be used.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE monitor_type ADD VALUE IF NOT EXISTS "
            "'DEFERRED_TAX_ACCURACY'"
        )

    op.add_column(
        "tax_master_version",
        sa.Column("deferred_tax_rate", sa.Numeric(20, 12), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_tax_master_version_deferred_tax_rate"),
        "tax_master_version",
        "deferred_tax_rate IS NULL OR deferred_tax_rate BETWEEN 0 AND 1",
    )

    if existing_rule is None:
        op.get_bind().execute(
            sa.text(
                "INSERT INTO rule_version ("
                "rule_code, version, status, effective_from, definition, "
                "change_reason, published_at, approved_by"
                ") VALUES ("
                ":rule_code, :version, 'PUBLISHED', DATE '2000-01-01', "
                "CAST(:definition AS jsonb), :change_reason, CURRENT_TIMESTAMP, "
                ":approved_by"
                ")"
            ),
            {
                "rule_code": RULE_CODE,
                "version": RULE_VERSION,
                "definition": json.dumps(
                    _manifest_definition(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "change_reason": RULE_CHANGE_REASON,
                "approved_by": RULE_APPROVER,
            },
        )


def downgrade() -> None:
    _assert_safe_downgrade()

    op.get_bind().execute(
        sa.text(
            "DELETE FROM rule_version "
            "WHERE rule_code = :rule_code AND version = :version "
            "AND definition -> 'migration_provenance' = CAST(:provenance AS jsonb)"
        ),
        {
            "rule_code": RULE_CODE,
            "version": RULE_VERSION,
            "provenance": json.dumps(
                MIGRATION_PROVENANCE,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )
    op.drop_constraint(
        op.f("ck_tax_master_version_deferred_tax_rate"),
        "tax_master_version",
        type_="check",
    )
    op.drop_column("tax_master_version", "deferred_tax_rate")

    # Enum labels are intentionally additive, matching 0010/0011. PostgreSQL has
    # no safe in-place value removal; the guards above ensure no deferred-tax rows
    # survive while retaining an idempotent downgrade/re-upgrade path.

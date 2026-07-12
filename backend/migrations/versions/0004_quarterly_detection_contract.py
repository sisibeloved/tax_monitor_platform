"""Persist exact quarterly rates and immutable detection evidence.

Revision ID: 0004_quarterly_detection
Revises: 0003_tax_master_governance
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256
import json

from alembic import op
import sqlalchemy as sa


revision: str = "0004_quarterly_detection"
down_revision: str | None = "0003_tax_master_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RULE_VERSION = "phase-1-reviewed"
RULE_CHANGE_REASON = "Fixed, reviewed Phase 1 quarterly formula manifest."
RULE_APPROVER = "phase-1-tax-review-board"
MIGRATION_PROVENANCE: dict[str, str] = {
    "revision": revision,
    "seed": f"QUARTERLY_V1:{RULE_VERSION}",
}
FORMULA_MANIFEST: dict[str, object] = {
    "schema_version": "QUARTERLY_V1",
    "rounding_mode": "ROUND_HALF_UP",
    "formulas": {
        "base_before_floor": (
            "cumulative_profit-received_dividends-fair_value_change-loss_carryforward"
        ),
        "cumulative_base": "max(base_before_floor,0)",
        "cumulative_tax_payable": "cumulative_base*tax_rate",
        "current_quarter_should_accrue": ("cumulative_tax_payable-prior_quarter_current_tax"),
        "current_quarter_difference": ("current_quarter_should_accrue-current_quarter_current_tax"),
        "current_tax_burden": "cumulative_tax_payable/cumulative_revenue",
        "tax_burden_deviation": ("current_tax_burden-historical_average_tax_burden"),
        "potential_adjustment": "other_payables_accrual+hesi_no_invoice",
        "potential_base": "max(base_before_floor+potential_adjustment,0)",
        "potential_tax_payable": "potential_base*tax_rate",
        "potential_tax_cost": "potential_tax_payable-cumulative_tax_payable",
    },
    "alert_boundaries": {
        "accrual_accuracy": "difference != 0",
        "tax_burden": "deviation >= 0.05 or deviation <= -0.05",
        "potential_tax_cost": "cost != 0",
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


def upgrade() -> None:
    existing_rule = op.get_bind().execute(
        sa.text(
            "SELECT id, status, effective_from, effective_to, definition, "
            "change_reason, published_at, approved_by FROM rule_version "
            "WHERE rule_code = 'QUARTERLY_V1' AND version = :version"
        ),
        {"version": RULE_VERSION},
    ).mappings().one_or_none()
    existing_rule_is_owned_seed = existing_rule is not None and dict(existing_rule) == {
        "id": existing_rule["id"],
        "status": "PUBLISHED",
        "effective_from": date(2000, 1, 1),
        "effective_to": None,
        "definition": _manifest_definition(),
        "change_reason": RULE_CHANGE_REASON,
        "published_at": existing_rule["published_at"],
        "approved_by": RULE_APPROVER,
    } and existing_rule["published_at"] is not None
    if existing_rule is not None and not existing_rule_is_owned_seed:
        raise RuntimeError(
            "refusing to overwrite preexisting fixed quarterly rule "
            f"QUARTERLY_V1:{RULE_VERSION} ({existing_rule['id']})"
        )

    op.add_column(
        "detection_record",
        sa.Column("tax_burden_rate", sa.Numeric(38, 12), nullable=True),
    )
    op.add_column(
        "detection_record",
        sa.Column("tax_burden_deviation", sa.Numeric(38, 12), nullable=True),
    )
    # The 0003 check requires every CALCULATED row to keep result_amount populated.
    # Remove it before moving legacy burden values into their dedicated columns.
    op.drop_constraint(
        op.f("ck_detection_record_calculation_state"),
        "detection_record",
        type_="check",
    )
    op.execute(
        """
        UPDATE detection_record
        SET tax_burden_rate = result_amount,
            tax_burden_deviation = difference_amount,
            result_amount = NULL,
            difference_amount = NULL
        WHERE monitor_type = 'TAX_BURDEN'
          AND calculation_status = 'CALCULATED'
          AND difference_amount IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE detection_record
        SET calculation_status = 'FAILED',
            result_amount = NULL,
            difference_amount = NULL,
            tax_burden_rate = NULL,
            tax_burden_deviation = NULL,
            not_calculated_reason = 'LEGACY_TAX_BURDEN_DEVIATION_MISSING',
            alert_code = NULL,
            direction = NULL
        WHERE monitor_type = 'TAX_BURDEN'
          AND calculation_status = 'CALCULATED'
          AND difference_amount IS NULL
          AND tax_burden_rate IS NULL
        """
    )
    op.create_check_constraint(
        op.f("ck_detection_record_calculation_state"),
        "detection_record",
        "(calculation_status = 'CALCULATED' AND not_calculated_reason IS NULL AND "
        "((monitor_type = 'TAX_BURDEN' AND result_amount IS NULL "
        "AND tax_burden_rate IS NOT NULL AND tax_burden_deviation IS NOT NULL) OR "
        "(monitor_type <> 'TAX_BURDEN' AND result_amount IS NOT NULL "
        "AND tax_burden_rate IS NULL AND tax_burden_deviation IS NULL))) OR "
        "(calculation_status IN ('NOT_CALCULABLE', 'FAILED') "
        "AND result_amount IS NULL AND tax_burden_rate IS NULL "
        "AND tax_burden_deviation IS NULL AND not_calculated_reason IS NOT NULL)",
    )

    op.alter_column(
        "risk_case",
        "risk_amount",
        existing_type=sa.Numeric(38, 12),
        nullable=True,
    )
    op.add_column(
        "risk_case",
        sa.Column("risk_rate", sa.Numeric(38, 12), nullable=True),
    )
    op.execute(
        """
        UPDATE risk_case
        SET risk_rate = abs(risk_amount),
            risk_amount = NULL
        WHERE monitor_type = 'TAX_BURDEN'
        """
    )
    op.execute(
        """
        UPDATE risk_case
        SET risk_amount = abs(risk_amount)
        WHERE monitor_type <> 'TAX_BURDEN'
        """
    )
    op.create_check_constraint(
        op.f("ck_risk_case_monitor_value"),
        "risk_case",
        "(monitor_type = 'TAX_BURDEN' AND risk_amount IS NULL AND risk_rate IS NOT NULL) "
        "OR (monitor_type <> 'TAX_BURDEN' AND risk_amount IS NOT NULL AND risk_rate IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_risk_case_nonnegative_amount"),
        "risk_case",
        "risk_amount IS NULL OR risk_amount >= 0",
    )
    op.create_check_constraint(
        op.f("ck_risk_case_nonnegative_rate"),
        "risk_case",
        "risk_rate IS NULL OR risk_rate >= 0",
    )

    op.drop_constraint(
        "detection_record_run_id_fkey",
        "detection_record",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "detection_record_run_id_fkey",
        "detection_record",
        "monitoring_run",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION reject_detection_record_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable_detection_record: detection_record % cannot be changed',
                OLD.id USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_detection_record_immutable
        BEFORE UPDATE OR DELETE ON detection_record
        FOR EACH ROW EXECUTE FUNCTION reject_detection_record_change()
        """
    )

    if existing_rule is None:
        op.get_bind().execute(
            sa.text(
                """
                INSERT INTO rule_version (
                    rule_code, version, status, effective_from, definition,
                    change_reason, published_at, approved_by
                )
                VALUES (
                    'QUARTERLY_V1', :version, 'PUBLISHED', DATE '2000-01-01',
                    CAST(:definition AS jsonb), :change_reason, CURRENT_TIMESTAMP,
                    :approved_by
                )
                """
            ),
            {
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
    # Remove immutability before restoring the legacy cascading run relationship.
    op.execute("DROP FUNCTION IF EXISTS reject_detection_record_change() CASCADE")

    op.get_bind().execute(
        sa.text(
            "DELETE FROM rule_version "
            "WHERE rule_code = 'QUARTERLY_V1' AND version = :version "
            "AND definition -> 'migration_provenance' = CAST(:provenance AS jsonb) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM monitoring_run "
            "WHERE monitoring_run.rule_version_id = rule_version.id"
            ")"
        ),
        {
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
        "detection_record_run_id_fkey",
        "detection_record",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "detection_record_run_id_fkey",
        "detection_record",
        "monitoring_run",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        op.f("ck_risk_case_nonnegative_rate"),
        "risk_case",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_risk_case_nonnegative_amount"),
        "risk_case",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_risk_case_monitor_value"),
        "risk_case",
        type_="check",
    )
    op.execute(
        """
        UPDATE risk_case
        SET risk_amount = risk_rate
        WHERE monitor_type = 'TAX_BURDEN'
        """
    )
    op.drop_column("risk_case", "risk_rate")
    op.alter_column(
        "risk_case",
        "risk_amount",
        existing_type=sa.Numeric(38, 12),
        nullable=False,
    )

    op.drop_constraint(
        op.f("ck_detection_record_calculation_state"),
        "detection_record",
        type_="check",
    )
    op.execute(
        """
        UPDATE detection_record
        SET result_amount = tax_burden_rate,
            difference_amount = tax_burden_deviation
        WHERE monitor_type = 'TAX_BURDEN'
          AND calculation_status = 'CALCULATED'
        """
    )
    op.create_check_constraint(
        op.f("ck_detection_record_calculation_state"),
        "detection_record",
        "(calculation_status = 'CALCULATED' AND result_amount IS NOT NULL AND "
        "not_calculated_reason IS NULL) OR "
        "(calculation_status = 'NOT_CALCULABLE' AND result_amount IS NULL AND "
        "not_calculated_reason IS NOT NULL) OR "
        "(calculation_status = 'FAILED' AND result_amount IS NULL AND "
        "not_calculated_reason IS NOT NULL)",
    )
    op.drop_column("detection_record", "tax_burden_deviation")
    op.drop_column("detection_record", "tax_burden_rate")

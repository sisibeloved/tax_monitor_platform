"""Set the tax burden rate to zero when cumulative revenue is nonpositive.

Revision ID: 0018_nonpositive_revenue_tax_burden
Revises: 0017_strict_rls_runtime
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from hashlib import sha256
import json

from alembic import op
import sqlalchemy as sa


revision: str = "0018_nonpositive_revenue_tax_burden"
down_revision: str | None = "0017_strict_rls_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RULE_CODE = "QUARTERLY_V1"
_RULE_VERSION = "phase-1-reviewed"
_OLD_FORMULA = "cumulative_tax_payable/cumulative_revenue"
_NEW_FORMULA = "0 if cumulative_revenue<=0 else cumulative_tax_payable/cumulative_revenue"
_OLD_CHANGE_REASON = "Fixed, reviewed Phase 1 quarterly formula manifest."
_NEW_CHANGE_REASON = (
    "Confirmed cumulative tax burden rate fallback for nonpositive cumulative revenue."
)
_OLD_APPROVER = "phase-1-tax-review-board"
_NEW_APPROVER = "business-rule-confirmation-2026-07-14"


def _updated_definition(
    definition: dict[str, object],
    *,
    expected_formula: str,
    replacement_formula: str,
    add_revision: bool,
) -> dict[str, object]:
    updated = deepcopy(definition)
    manifest = updated.get("formula_manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError("QUARTERLY_RULE_MANIFEST_INVALID: formula_manifest is missing")
    formulas = manifest.get("formulas")
    if not isinstance(formulas, dict) or formulas.get("current_tax_burden") != expected_formula:
        raise RuntimeError(
            "QUARTERLY_RULE_MANIFEST_UNEXPECTED: current_tax_burden formula does not match"
        )

    formulas["current_tax_burden"] = replacement_formula
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    updated["formula_manifest_sha256"] = sha256(canonical).hexdigest()
    if add_revision:
        updated["business_rule_revision"] = revision
    else:
        updated.pop("business_rule_revision", None)
    return updated


def _rule_row() -> dict[str, object]:
    row = op.get_bind().execute(
        sa.text(
            "SELECT id, definition FROM rule_version "
            "WHERE rule_code = :rule_code AND version = :version FOR UPDATE"
        ),
        {"rule_code": _RULE_CODE, "version": _RULE_VERSION},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("QUARTERLY_RULE_MISSING: reviewed Phase 1 rule was not found")
    return dict(row)


def _store_definition(
    *,
    rule_id: object,
    definition: dict[str, object],
    change_reason: str,
    approved_by: str,
) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE rule_version SET definition = CAST(:definition AS jsonb), "
            "change_reason = :change_reason, approved_by = :approved_by "
            "WHERE id = :rule_id"
        ),
        {
            "rule_id": rule_id,
            "definition": json.dumps(definition, ensure_ascii=False, sort_keys=True),
            "change_reason": change_reason,
            "approved_by": approved_by,
        },
    )


def upgrade() -> None:
    # This revision identifier is 35 characters; Alembic creates version_num
    # as VARCHAR(32) unless a migration expands it before stamping the revision.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    row = _rule_row()
    definition = row["definition"]
    if not isinstance(definition, dict):
        raise RuntimeError("QUARTERLY_RULE_MANIFEST_INVALID: definition is not an object")
    updated = _updated_definition(
        definition,
        expected_formula=_OLD_FORMULA,
        replacement_formula=_NEW_FORMULA,
        add_revision=True,
    )
    _store_definition(
        rule_id=row["id"],
        definition=updated,
        change_reason=_NEW_CHANGE_REASON,
        approved_by=_NEW_APPROVER,
    )


def downgrade() -> None:
    # Keep the expanded version column: shrinking it would make a later
    # downgrade/re-upgrade unable to stamp this 35-character revision safely.
    row = _rule_row()
    definition = row["definition"]
    if not isinstance(definition, dict):
        raise RuntimeError("QUARTERLY_RULE_MANIFEST_INVALID: definition is not an object")
    restored = _updated_definition(
        definition,
        expected_formula=_NEW_FORMULA,
        replacement_formula=_OLD_FORMULA,
        add_revision=False,
    )
    _store_definition(
        rule_id=row["id"],
        definition=restored,
        change_reason=_OLD_CHANGE_REASON,
        approved_by=_OLD_APPROVER,
    )

from __future__ import annotations

from collections.abc import Callable, Iterable
import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import cast

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Constraint, ForeignKeyConstraint, UniqueConstraint

from tax_risk.persistence import Base


EXPECTED_TABLES = {
    "income_tax_refund_scan_result",
    "income_tax_refund_target",
    "income_tax_refund_writeback",
    "sap_gl_line_observation",
    "sap_refund_evidence_batch",
}


def test_refund_models_register_stable_keys_and_company_safe_foreign_keys() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)

    target = Base.metadata.tables["income_tax_refund_target"]
    assert {"company_id", "refund_tax_year"} in _unique_column_sets(target.constraints)

    gl_line = Base.metadata.tables["sap_gl_line_observation"]
    assert {
        "source_batch_key",
        "client",
        "ledger",
        "company_id",
        "fiscal_year",
        "document_number",
        "line_item",
    } in _unique_column_sets(gl_line.constraints)
    assert _foreign_key_columns(gl_line.constraints, "fk_sap_refund_gl_evidence_batch") == {
        "source_batch_key"
    }

    scan = Base.metadata.tables["income_tax_refund_scan_result"]
    assert {"target_id", "scan_period"} in _unique_column_sets(scan.constraints)
    assert _foreign_key_columns(scan.constraints, "fk_refund_scan_target_company") == {
        "target_id",
        "company_id",
    }
    assert _foreign_key_columns(scan.constraints, "fk_refund_scan_matched_line_company") == {
        "matched_line_id",
        "company_id",
    }
    checks = _check_sql(scan.constraints)
    assert "REFUND_BOOKED_TO_WRONG_ACCOUNT" in checks
    assert "AMBIGUOUS_REFUND_MATCH" in checks
    assert "structured_output -> 'completeness' = 'true'::jsonb" in checks
    assert "structured_output -> 'source_batch_key'" in checks

    writeback = Base.metadata.tables["income_tax_refund_writeback"]
    assert {"target_id"} in _unique_column_sets(writeback.constraints)
    assert {"idempotency_key"} in _unique_column_sets(writeback.constraints)


def test_refund_migration_renders_enum_tables_and_forced_rls_offline() -> None:
    module = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        cast(Callable[[], None], module.upgrade)()

    sql = output.getvalue()
    assert "INCOME_TAX_REFUND_ACCOUNT_ACCURACY" in sql
    assert sql.count("CREATE TABLE") == len(EXPECTED_TABLES)
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(EXPECTED_TABLES)
    assert sql.count("FORCE ROW LEVEL SECURITY") == len(EXPECTED_TABLES)
    for table_name in EXPECTED_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
        assert f'CREATE POLICY "{table_name}_company_scope"' in sql


def test_ambiguous_match_alert_migration_updates_the_classification_constraint() -> None:
    module = _alert_migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        cast(Callable[[], None], module.upgrade)()

    sql = output.getvalue()
    assert module.revision == "0023_refund_ambiguous_match_alert"
    assert module.down_revision == "0022_refund_taxes_payable_priority"
    assert "DROP CONSTRAINT ck_income_tax_refund_scan_result_classification_state" in sql
    assert "AMBIGUOUS_REFUND_MATCH" in sql


def _migration_module() -> ModuleType:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0020_income_tax_refund_accuracy.py"
    )
    spec = importlib.util.spec_from_file_location("income_tax_refund_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alert_migration_module() -> ModuleType:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0023_refund_ambiguous_match_alert.py"
    )
    spec = importlib.util.spec_from_file_location(
        "refund_ambiguous_match_alert_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unique_column_sets(constraints: Iterable[Constraint]) -> set[frozenset[str]]:
    return {
        frozenset(column.name for column in constraint.columns)
        for constraint in constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_key_columns(constraints: Iterable[Constraint], name: str) -> set[str]:
    constraint = next(
        item for item in constraints if isinstance(item, ForeignKeyConstraint) and item.name == name
    )
    return {column.name for column in constraint.columns}


def _check_sql(constraints: Iterable[Constraint]) -> str:
    return "\n".join(
        str(constraint.sqltext)
        for constraint in constraints
        if isinstance(constraint, CheckConstraint)
    )

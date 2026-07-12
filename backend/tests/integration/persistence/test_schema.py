from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB, NUMERIC, TIMESTAMP, UUID
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from tax_risk.config import Settings


EXPECTED_TABLES = {
    "company",
    "ingest_batch",
    "ingest_error",
    "source_record",
    "tax_master_version",
    "accounting_snapshot",
    "snapshot_source",
    "snapshot_set",
    "snapshot_set_member",
    "rule_version",
    "monitoring_run",
    "detection_record",
    "risk_case",
    "review_action",
    "audit_event",
}
BACKEND_ROOT = Path(__file__).resolve().parents[3]
PYTEST_SCHEMA_PATTERN = re.compile(r"tax_risk_pytest_[0-9a-f]{32}")
PYTEST_SCHEMA_MARKER = "tax_risk_pytest_owned_v1"


def test_persistence_engine_uses_marker_owned_random_pytest_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
        schema_marker = connection.execute(
            text(
                """
                SELECT obj_description(namespace.oid, 'pg_namespace')
                FROM pg_namespace AS namespace
                WHERE namespace.nspname = current_schema()
                """
            )
        ).scalar_one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert PYTEST_SCHEMA_PATTERN.fullmatch(schema_name), schema_name
    assert schema_marker == PYTEST_SCHEMA_MARKER
    assert revision == "0003_tax_master_governance"


def _column(engine: Engine, table_name: str, column_name: str) -> dict[str, object]:
    columns = inspect(engine).get_columns(table_name)
    return next(column for column in columns if column["name"] == column_name)


def _enum_labels(engine: Engine, enum_name: str) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    """
                    SELECT enum_value.enumlabel
                    FROM pg_enum AS enum_value
                    JOIN pg_type AS enum_type ON enum_type.oid = enum_value.enumtypid
                    JOIN pg_namespace AS enum_namespace
                      ON enum_namespace.oid = enum_type.typnamespace
                    WHERE enum_type.typname = :enum_name
                      AND enum_namespace.nspname = current_schema()
                    ORDER BY enum_value.enumsortorder
                    """
                ),
                {"enum_name": enum_name},
            ).scalars()
        )


def test_control_plane_has_every_required_table_and_uuid_primary_key(engine: Engine) -> None:
    inspector = inspect(engine)

    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    for table_name in EXPECTED_TABLES:
        primary_key = inspector.get_pk_constraint(table_name)
        assert primary_key["constrained_columns"] == ["id"]
        assert isinstance(_column(engine, table_name, "id")["type"], UUID)


def test_schema_uses_postgresql_enums_and_timezone_aware_audit_fields(engine: Engine) -> None:
    enum_names = {enum["name"] for enum in inspect(engine).get_enums()}

    assert {
        "company_lifecycle",
        "ingest_batch_status",
        "ingest_mode",
        "version_status",
        "snapshot_status",
        "snapshot_set_status",
        "monitoring_run_status",
        "calculation_status",
        "risk_case_status",
    } <= enum_names

    for table_name in EXPECTED_TABLES - {"audit_event"}:
        created_at = _column(engine, table_name, "created_at")
        assert isinstance(created_at["type"], TIMESTAMP)
        assert created_at["type"].timezone is True

    occurred_at = _column(engine, "audit_event", "occurred_at")
    assert isinstance(occurred_at["type"], TIMESTAMP)
    assert occurred_at["type"].timezone is True

    master_updated_at = _column(engine, "company", "master_data_updated_at")
    assert isinstance(master_updated_at["type"], TIMESTAMP)
    assert master_updated_at["type"].timezone is True
    assert master_updated_at["nullable"] is False


@pytest.mark.parametrize(
    ("enum_name", "expected_labels"),
    [
        (
            "ingest_batch_status",
            ["RECEIVED", "VALIDATING", "SUCCEEDED", "PARTIAL", "FAILED"],
        ),
        ("snapshot_status", ["DRAFT", "VALIDATED", "PUBLISHED"]),
        (
            "monitor_type",
            ["ACCRUAL_ACCURACY", "TAX_BURDEN", "POTENTIAL_TAX_COST"],
        ),
        (
            "risk_case_status",
            [
                "NEW",
                "ASSIGNED",
                "PENDING_COMPANY_CONFIRMATION",
                "PENDING_ADJUSTMENT",
                "ADJUSTED_PENDING_REVIEW",
                "CLOSED",
                "GROUP_REVIEW",
                "EVIDENCE_REQUIRED",
            ],
        ),
    ],
)
def test_control_plane_enums_match_approved_phase_one_state_machines(
    engine: Engine,
    enum_name: str,
    expected_labels: list[str],
) -> None:
    assert _enum_labels(engine, enum_name) == expected_labels


def test_numeric_and_json_lineage_contracts_are_exact(engine: Engine) -> None:
    for table_name, column_name in {
        ("ingest_batch", "control_total"),
        ("source_record", "amount"),
        ("tax_master_version", "loss_carryforward"),
        ("accounting_snapshot", "control_total"),
        ("snapshot_source", "control_total"),
        ("detection_record", "input_amount"),
        ("detection_record", "result_amount"),
        ("detection_record", "difference_amount"),
        ("risk_case", "risk_amount"),
    }:
        numeric_type = _column(engine, table_name, column_name)["type"]
        assert isinstance(numeric_type, NUMERIC)
        assert (numeric_type.precision, numeric_type.scale) == (38, 12)

    for table_name, column_name in {
        ("tax_master_version", "tax_rate"),
        ("tax_master_version", "average_tax_burden_rate_3y"),
        ("detection_record", "rate_value"),
    }:
        rate_type = _column(engine, table_name, column_name)["type"]
        assert isinstance(rate_type, NUMERIC)
        assert (rate_type.precision, rate_type.scale) == (20, 12)

    for table_name in {
        "ingest_batch",
        "source_record",
        "tax_master_version",
        "accounting_snapshot",
        "snapshot_source",
        "detection_record",
        "risk_case",
    }:
        columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
        assert {"currency", "amount_scale"} <= columns

    for table_name, column_name in {
        ("source_record", "lineage"),
        ("snapshot_source", "lineage"),
        ("detection_record", "lineage"),
        ("detection_record", "formula_substitution"),
    }:
        assert isinstance(_column(engine, table_name, column_name)["type"], JSONB)


def test_tax_master_governance_has_strict_state_loss_and_no_legacy_defaults(
    engine: Engine,
) -> None:
    source_row = _column(engine, "tax_master_version", "source_row_number")
    uploaded_by = _column(engine, "tax_master_version", "uploaded_by")
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints("tax_master_version")
    }

    assert source_row["nullable"] is False
    assert source_row["default"] is None
    assert uploaded_by["nullable"] is False
    assert uploaded_by["default"] is None
    assert any("loss_carryforward >= 0" in sql for sql in checks.values())
    state_sql = next(sql for name, sql in checks.items() if "published_at_state" in name)
    assert "approved_by IS NOT NULL" in state_sql
    assert "approved_by IS NULL" in state_sql


def test_foreign_keys_cover_lineage_and_control_plane_relationships(engine: Engine) -> None:
    expected_foreign_keys = {
        "ingest_error": {("batch_id", "ingest_batch")},
        "source_record": {("batch_id", "ingest_batch"), ("company_id", "company")},
        "tax_master_version": {("company_id", "company"), ("source_batch_id", "ingest_batch")},
        "accounting_snapshot": {
            ("company_id", "company"),
            ("tax_master_version_id", "tax_master_version"),
        },
        "snapshot_source": {
            ("snapshot_id", "accounting_snapshot"),
            ("ingest_batch_id", "ingest_batch"),
        },
        "snapshot_set_member": {
            ("snapshot_set_id", "snapshot_set"),
            ("company_id", "company"),
            ("snapshot_id", "accounting_snapshot"),
        },
        "monitoring_run": {
            ("snapshot_set_id", "snapshot_set"),
            ("rule_version_id", "rule_version"),
        },
        "detection_record": {
            ("run_id", "monitoring_run"),
            ("company_id", "company"),
            ("snapshot_id", "accounting_snapshot"),
            ("rule_version_id", "rule_version"),
            ("tax_master_version_id", "tax_master_version"),
        },
        "risk_case": {("company_id", "company"), ("latest_detection_id", "detection_record")},
        "review_action": {("risk_case_id", "risk_case")},
    }

    inspector = inspect(engine)
    for table_name, expected in expected_foreign_keys.items():
        actual = {
            (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        assert expected <= actual


def test_company_consistent_lineage_uses_composite_foreign_keys(engine: Engine) -> None:
    inspector = inspect(engine)
    accounting_snapshot_foreign_keys = inspector.get_foreign_keys("accounting_snapshot")
    detection_foreign_keys = inspector.get_foreign_keys("detection_record")

    assert any(
        foreign_key["constrained_columns"] == ["tax_master_version_id", "company_id"]
        and foreign_key["referred_table"] == "tax_master_version"
        and foreign_key["referred_columns"] == ["id", "company_id"]
        for foreign_key in accounting_snapshot_foreign_keys
    )
    assert any(
        foreign_key["constrained_columns"] == ["snapshot_id", "company_id", "tax_master_version_id"]
        and foreign_key["referred_table"] == "accounting_snapshot"
        and foreign_key["referred_columns"] == ["id", "company_id", "tax_master_version_id"]
        for foreign_key in detection_foreign_keys
    )


def test_quarterly_detection_schema_freezes_master_and_outcome_fields(engine: Engine) -> None:
    accounting_snapshot_columns = {
        column["name"]: column for column in inspect(engine).get_columns("accounting_snapshot")
    }
    detection_columns = {
        column["name"]: column for column in inspect(engine).get_columns("detection_record")
    }

    assert accounting_snapshot_columns["tax_master_version_id"]["nullable"] is False
    assert detection_columns["tax_master_version_id"]["nullable"] is False
    assert detection_columns["alert_code"]["nullable"] is True
    assert detection_columns["direction"]["nullable"] is True

    review_status_type = _column(engine, "review_action", "from_status")["type"]
    assert getattr(review_status_type, "name", None) == "risk_case_status"


def test_repositories_import_cleanly_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tax_risk.persistence.repositories import UnitOfWork; "
            "assert UnitOfWork.__name__ == 'UnitOfWork'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_models_import_registers_every_table_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tax_risk.persistence.models import Base; "
            f"expected = {EXPECTED_TABLES!r}; "
            "actual = set(Base.metadata.tables); "
            "assert actual == expected, f'expected {expected!r}, got {actual!r}'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "direct_import",
    [
        "from tax_risk.persistence.ingest_models import Company; "
        "from tax_risk.persistence.models import Base",
        "from tax_risk.persistence.repositories import UnitOfWork; "
        "from tax_risk.persistence.models import Base",
        "from tax_risk.db import Base",
    ],
    ids=["focused-model", "repositories", "db"],
)
def test_every_persistence_import_path_registers_complete_metadata(
    direct_import: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{direct_import}; "
            f"expected = {EXPECTED_TABLES!r}; "
            "actual = set(Base.metadata.tables); "
            "assert actual == expected, f'expected {expected!r}, got {actual!r}'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND_ROOT / "alembic.ini"),
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        env=os.environ | {"DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )


@contextmanager
def _owned_migration_schema() -> Iterator[tuple[str, Engine]]:
    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_pytest_{uuid4().hex}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    isolated_engine: Engine | None = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
            quoted = connection.dialect.identifier_preparer.quote(schema_name)
            connection.exec_driver_sql(
                f"COMMENT ON SCHEMA {quoted} IS '{PYTEST_SCHEMA_MARKER}'"
            )
        database_url = base_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        ).render_as_string(hide_password=False)
        isolated_engine = create_engine(database_url, poolclass=NullPool)
        yield database_url, isolated_engine
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        with admin_engine.begin() as connection:
            marker = connection.execute(
                text(
                    """
                    SELECT obj_description(oid, 'pg_namespace')
                    FROM pg_namespace WHERE nspname = :schema_name
                    """
                ),
                {"schema_name": schema_name},
            ).scalar_one_or_none()
            assert marker == PYTEST_SCHEMA_MARKER
            connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


def _insert_pre_0003_tax_master(
    engine: Engine,
    *,
    loss: str,
    status: str,
    published_at_sql: str,
) -> None:
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:code, 'Legacy Company') RETURNING id
                """
            ),
            {"code": f"LEGACY-{uuid4().hex}"},
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'TAX_MASTER', :key, 'tax_master', 'SUCCEEDED', now(),
                    DATE '2026-03-31', 'FULL', '1', 'CNY', 2,
                    1, 1, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"legacy-{uuid4().hex}"},
        ).scalar_one()
        connection.execute(
            text(
                f"""
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, data, published_at
                ) VALUES (
                    :company_id, :batch_id, DATE '2026-01-01', 'legacy-v1', :status,
                    0.25, :loss, 0.10, 'CNY', 2, '{{}}'::jsonb, {published_at_sql}
                )
                """
            ),
            {
                "company_id": company_id,
                "batch_id": batch_id,
                "status": status,
                "loss": loss,
            },
        )


def test_alembic_current_accepts_a_percent_encoded_database_url(
    isolated_database_url: str,
) -> None:
    encoded_url = (
        make_url(isolated_database_url)
        .update_query_dict({"application_name": "tax risk"})
        .render_as_string(hide_password=False)
        .replace("application_name=tax+risk", "application_name=tax%20risk")
    )
    assert "application_name=tax%20risk" in encoded_url
    completed = _run_alembic(encoded_url, "current")

    assert completed.returncode == 0, completed.stderr
    assert "0003_tax_master_governance (head)" in completed.stdout


def test_alembic_check_and_round_trip_stay_in_the_isolated_schema(
    isolated_database_url: str,
) -> None:
    before = _run_alembic(isolated_database_url, "check")
    assert before.returncode == 0, before.stderr

    downgrade = _run_alembic(isolated_database_url, "downgrade", "base")
    assert downgrade.returncode == 0, downgrade.stderr

    upgrade = _run_alembic(isolated_database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    after = _run_alembic(isolated_database_url, "check")
    assert after.returncode == 0, after.stderr


def test_database_is_at_control_plane_revision(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0003_tax_master_governance"


def test_0003_backfills_legacy_approval_audit_then_removes_insert_defaults() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0002 = _run_alembic(
            database_url,
            "upgrade",
            "0002_company_master_freshness",
        )
        assert upgrade_0002.returncode == 0, upgrade_0002.stderr
        _insert_pre_0003_tax_master(
            isolated_engine,
            loss="0",
            status="PUBLISHED",
            published_at_sql="now()",
        )

        upgrade_0003 = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_0003.returncode == 0, upgrade_0003.stderr
        with isolated_engine.connect() as connection:
            governed = connection.execute(
                text(
                    """
                    SELECT approved_by, uploaded_by, source_row_number
                    FROM tax_master_version WHERE version = 'legacy-v1'
                    """
                )
            ).mappings().one()
        columns = {
            column["name"]: column
            for column in inspect(isolated_engine).get_columns("tax_master_version")
        }

        assert governed == {
            "approved_by": "legacy-migration",
            "uploaded_by": "legacy-migration",
            "source_row_number": 2,
        }
        assert columns["uploaded_by"]["default"] is None
        assert columns["source_row_number"]["default"] is None

        downgrade = _run_alembic(
            database_url,
            "downgrade",
            "0002_company_master_freshness",
        )
        assert downgrade.returncode == 0, downgrade.stderr
        reupgrade = _run_alembic(database_url, "upgrade", "head")
        assert reupgrade.returncode == 0, reupgrade.stderr


def test_0003_refuses_historical_negative_loss_without_modifying_it() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0002 = _run_alembic(
            database_url,
            "upgrade",
            "0002_company_master_freshness",
        )
        assert upgrade_0002.returncode == 0, upgrade_0002.stderr
        _insert_pre_0003_tax_master(
            isolated_engine,
            loss="-0.01",
            status="DRAFT",
            published_at_sql="NULL",
        )

        upgrade_0003 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0003.returncode != 0
        with isolated_engine.connect() as connection:
            loss = connection.execute(
                text(
                    "SELECT loss_carryforward FROM tax_master_version "
                    "WHERE version = 'legacy-v1'"
                )
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert loss == Decimal("-0.010000000000")
        assert revision == "0002_company_master_freshness"


def test_0002_backfills_existing_company_from_historical_lifecycle_timestamp() -> None:
    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_pytest_{uuid4().hex}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
            quoted = connection.dialect.identifier_preparer.quote(schema_name)
            connection.exec_driver_sql(f"COMMENT ON SCHEMA {quoted} IS '{PYTEST_SCHEMA_MARKER}'")
        database_url = base_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        ).render_as_string(hide_password=False)
        first_upgrade = _run_alembic(database_url, "upgrade", "0001_control_plane")
        assert first_upgrade.returncode == 0, first_upgrade.stderr
        isolated_engine = create_engine(database_url, poolclass=NullPool)
        try:
            with isolated_engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO company (
                            company_code, company_name, lifecycle_changed_at,
                            created_at, updated_at
                        ) VALUES (
                            'HISTORICAL', 'Historical Company',
                            TIMESTAMPTZ '2020-02-03 04:05:06+00',
                            TIMESTAMPTZ '2019-01-01 00:00:00+00',
                            TIMESTAMPTZ '2021-01-01 00:00:00+00'
                        )
                        """
                    )
                )
            second_upgrade = _run_alembic(database_url, "upgrade", "head")
            assert second_upgrade.returncode == 0, second_upgrade.stderr
            with isolated_engine.connect() as connection:
                backfilled = connection.execute(
                    text(
                        "SELECT master_data_updated_at FROM company "
                        "WHERE company_code = 'HISTORICAL'"
                    )
                ).scalar_one()
            assert backfilled.isoformat() == "2020-02-03T04:05:06+00:00"

            downgrade = _run_alembic(database_url, "downgrade", "0001_control_plane")
            assert downgrade.returncode == 0, downgrade.stderr
            assert "master_data_updated_at" not in {
                column["name"] for column in inspect(isolated_engine).get_columns("company")
            }
            final_upgrade = _run_alembic(database_url, "upgrade", "head")
            assert final_upgrade.returncode == 0, final_upgrade.stderr
        finally:
            isolated_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            marker = connection.execute(
                text(
                    """
                    SELECT obj_description(oid, 'pg_namespace')
                    FROM pg_namespace WHERE nspname = :schema_name
                    """
                ),
                {"schema_name": schema_name},
            ).scalar_one_or_none()
            assert marker == PYTEST_SCHEMA_MARKER
            connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()

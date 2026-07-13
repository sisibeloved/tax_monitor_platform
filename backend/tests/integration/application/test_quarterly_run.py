from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, getcontext
from functools import partial
from hashlib import sha256
import json
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tax_risk.application.quarterly_runs import (
    QuarterlyRunError,
    QuarterlyRunService,
)
import tax_risk.application.quarterly_runs as quarterly_runs_application
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


PERIOD = date(2026, 6, 30)
METRICS = (
    "cumulative_profit",
    "received_dividends",
    "fair_value_change",
    "cumulative_revenue",
    "prior_quarter_current_tax",
    "current_quarter_current_tax",
    "other_payables_accrual",
    "hesi_no_invoice",
)
DEFAULT_VALUES = {
    "cumulative_profit": Decimal("1000"),
    "received_dividends": Decimal("0"),
    "fair_value_change": Decimal("0"),
    "cumulative_revenue": Decimal("100"),
    "prior_quarter_current_tax": Decimal("0"),
    "current_quarter_current_tax": Decimal("0"),
    "other_payables_accrual": Decimal("100"),
    "hesi_no_invoice": Decimal("0"),
}


@dataclass(frozen=True, slots=True)
class QuarterlySeed:
    company_id: UUID
    company_code: str
    snapshot_id: UUID
    snapshot_set_id: UUID
    rule_version_id: UUID
    tax_master_version_id: UUID
    run_id: UUID
    secondary_company_id: UUID
    secondary_snapshot_id: UUID


@pytest.fixture
def resources(
    isolated_database_url: str,
) -> Iterator[tuple[Callable[[], UnitOfWork], Engine]]:
    database_engine, factory = create_session_factory(isolated_database_url)
    try:
        yield partial(UnitOfWork, factory), database_engine
    finally:
        with database_engine.begin() as connection:
            connection.execute(text("DELETE FROM business_entertainment_case_detail"))
            connection.execute(text("DELETE FROM semantic_evidence_task"))
            connection.execute(
                text(
                    "ALTER TABLE semantic_detection_record "
                    "DISABLE TRIGGER trg_semantic_detection_immutable"
                )
            )
            connection.execute(text("DELETE FROM semantic_detection_record"))
            connection.execute(
                text(
                    "ALTER TABLE semantic_detection_record "
                    "ENABLE TRIGGER trg_semantic_detection_immutable"
                )
            )
            connection.execute(text("DELETE FROM risk_case"))
            connection.execute(
                text("ALTER TABLE detection_record DISABLE TRIGGER trg_detection_record_immutable")
            )
            connection.execute(text("DELETE FROM detection_record"))
            connection.execute(
                text("ALTER TABLE detection_record ENABLE TRIGGER trg_detection_record_immutable")
            )
            connection.execute(text("DELETE FROM monitoring_run_company"))
            connection.execute(text("DELETE FROM monitoring_run"))
            connection.execute(
                text(
                    "DELETE FROM rule_version WHERE rule_code = 'QUARTERLY_V1' "
                    "AND version <> 'phase-1-reviewed'"
                )
            )
        database_engine.dispose()


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _lineage(
    token: str,
    values: Mapping[str, Decimal],
    *,
    master_id: UUID,
    master_batch_id: UUID,
    master_version: str,
    master_checksum: str,
    source_row_number: int,
    tax_rate: Decimal,
    loss_carryforward: Decimal,
    average_tax_burden: Decimal,
    include_source_metadata: bool = False,
    imported_at: str = "2026-07-01T09:45:00Z",
    extraction_time: str = "2026-07-01T08:15:30Z",
) -> dict[str, object]:
    tax_master: dict[str, object] = {
        "id": str(master_id),
        "version": master_version,
        "source_batch_id": str(master_batch_id),
        "source_checksum": master_checksum,
        "source_row_number": source_row_number,
        "valid_from": "2026-01-01",
        "valid_to": None,
        "tax_rate": format(tax_rate, "f"),
        "loss_carryforward": format(loss_carryforward, "f"),
        "three_year_average_tax_burden": format(average_tax_burden, "f"),
        "currency": "CNY",
        "amount_scale": 2,
    }
    sources: list[dict[str, object]] = [{"source": "SAP", "version": token}]
    if include_source_metadata:
        tax_master |= {
            "source_file_name": "tax-master.xlsx",
            "imported_at": imported_at,
        }
        sources = [
            {
                "batch": {
                    "id": str(uuid4()),
                    "source": "SAP",
                    "source_batch_key": f"SAP-{token}",
                    "dataset_code": "quarterly_metric",
                    "extraction_time": extraction_time,
                    "payload_ref": "sap-quarterly-2026-q2.csv",
                }
            }
        ]
    return {
        "schema_version": (
            "quarterly-accounting-snapshot-v2"
            if include_source_metadata
            else "quarterly-accounting-snapshot-v1"
        ),
        "metrics": [
            {
                "metric_code": metric,
                "amount": format(values[metric], "f"),
                "source_record": {
                    "source_record_key": f"{token}:{metric}",
                    "batch_id": str(uuid4()),
                    "lineage": {"row_number": index + 2, "worksheet": "Q2"},
                },
            }
            for index, metric in enumerate(METRICS)
        ],
        "sources": sources,
        "tax_master": tax_master,
    }


def _seed_quarterly_case(
    engine: Engine,
    *,
    values: Mapping[str, Decimal] | None = None,
    tax_rate: Decimal = Decimal("0.25"),
    loss_carryforward: Decimal = Decimal("0"),
    average_tax_burden: Decimal = Decimal("0.08"),
    include_source_metadata: bool = False,
    lineage_imported_at: str = "2026-07-01T09:45:00Z",
    lineage_extraction_time: str = "2026-07-01T08:15:30Z",
) -> QuarterlySeed:
    token = uuid4().hex
    target_values = DEFAULT_VALUES | dict(values or {})
    with engine.begin() as connection:
        rule_version_id = connection.execute(
            text(
                """
                SELECT id
                FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1'
                  AND status = 'PUBLISHED'
                  AND effective_from <= :period
                  AND (effective_to IS NULL OR effective_to >= :period)
                ORDER BY effective_from DESC, id
                LIMIT 1
                """
            ),
            {"period": PERIOD},
        ).scalar_one()
        master_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                )
                VALUES (
                    'TAX_MASTER_XLSX', :batch_key, 'tax_master', 'SUCCEEDED', now(),
                    :period, 'FULL', 'tax-master-v1', 'CNY', 2,
                    100, 100, 0, 0, :checksum
                )
                RETURNING id
                """
            ),
            {
                "batch_key": f"quarterly-master-{token}",
                "period": PERIOD,
                "checksum": _digest(f"master:{token}"),
            },
        ).scalar_one()
        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:set_key, :period, 'DRAFT', 100)
                RETURNING id
                """
            ),
            {"set_key": f"quarterly-set-{token}", "period": PERIOD},
        ).scalar_one()

        targets: list[tuple[UUID, str, UUID, UUID]] = []
        for index in range(100):
            company_code = f"QR-{token}-{index:03d}"
            company_id = connection.execute(
                text(
                    """
                    INSERT INTO company (company_code, company_name, lifecycle)
                    VALUES (:code, :name, 'ACTIVE')
                    RETURNING id
                    """
                ),
                {"code": company_code, "name": f"Quarterly company {index}"},
            ).scalar_one()
            master_version = f"v-{token}-{index}"
            master_checksum = _digest(f"master-row:{token}:{index}")
            source_row_number = index + 2
            master_id = connection.execute(
                text(
                    """
                    INSERT INTO tax_master_version (
                        company_id, source_batch_id, valid_from, version, status,
                        tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                        currency, amount_scale, source_file_name, source_checksum,
                        source_row_number, uploaded_by, data, published_at, approved_by,
                        created_at
                    )
                    VALUES (
                        :company_id, :source_batch_id, '2026-01-01', :version,
                        'PUBLISHED', :tax_rate, :loss, :average, 'CNY', 2,
                        'tax-master.xlsx', :checksum, :row_number, 'maker',
                        '{}'::jsonb, now(), 'reviewer',
                        TIMESTAMPTZ '2026-07-01 09:45:00+00'
                    )
                    RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "source_batch_id": master_batch_id,
                    "version": master_version,
                    "tax_rate": tax_rate,
                    "loss": loss_carryforward,
                    "average": average_tax_burden,
                    "checksum": master_checksum,
                    "row_number": source_row_number,
                },
            ).scalar_one()
            lineage = (
                _lineage(
                    token,
                    target_values,
                    master_id=master_id,
                    master_batch_id=master_batch_id,
                    master_version=master_version,
                    master_checksum=master_checksum,
                    source_row_number=source_row_number,
                    tax_rate=tax_rate,
                    loss_carryforward=loss_carryforward,
                    average_tax_burden=average_tax_burden,
                    include_source_metadata=include_source_metadata,
                    imported_at=lineage_imported_at,
                    extraction_time=lineage_extraction_time,
                )
                if index < 2
                else {"metrics": []}
            )
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO accounting_snapshot (
                        company_id, tax_master_version_id, period,
                        source_version_set_hash, status, currency, amount_scale,
                        record_count, control_total, checksum, lineage, published_at
                    )
                    VALUES (
                        :company_id, :master_id, :period, :source_hash,
                        'PUBLISHED', 'CNY', 2, :record_count, :control_total,
                        :checksum, CAST(:lineage AS jsonb), now()
                    )
                    RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "master_id": master_id,
                    "period": PERIOD,
                    "source_hash": _digest(f"snapshot-sources:{token}:{index}"),
                    "record_count": len(METRICS) if index < 2 else 0,
                    "control_total": (
                        sum(target_values.values(), Decimal("0")) if index < 2 else 0
                    ),
                    "checksum": _digest(f"snapshot:{token}:{index}"),
                    "lineage": json.dumps(lineage),
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set_member (
                        snapshot_set_id, company_id, snapshot_id
                    )
                    VALUES (:set_id, :company_id, :snapshot_id)
                    """
                ),
                {
                    "set_id": snapshot_set_id,
                    "company_id": company_id,
                    "snapshot_id": snapshot_id,
                },
            )
            if index < 2:
                targets.append((company_id, company_code, master_id, snapshot_id))

        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :set_id"),
            {"set_id": snapshot_set_id},
        )
        assert len(targets) == 2
        company_id, company_code, master_id, snapshot_id = targets[0]
        secondary_company_id, _, _, secondary_snapshot_id = targets[1]
        run_id = _insert_run(
            connection,
            token=token,
            snapshot_set_id=snapshot_set_id,
            rule_version_id=rule_version_id,
        )
    return QuarterlySeed(
        company_id=company_id,
        company_code=company_code,
        snapshot_id=snapshot_id,
        snapshot_set_id=snapshot_set_id,
        rule_version_id=rule_version_id,
        tax_master_version_id=master_id,
        run_id=run_id,
        secondary_company_id=secondary_company_id,
        secondary_snapshot_id=secondary_snapshot_id,
    )


def _insert_run(
    connection,
    *,
    token: str,
    snapshot_set_id: UUID,
    rule_version_id: UUID,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO monitoring_run (
                run_key, run_type, snapshot_set_id, rule_version_id, status,
                fiscal_year, quarter, requested_company_count
            )
            VALUES (
                :run_key, 'QUARTERLY', :snapshot_set_id, :rule_version_id,
                'RUNNING', 2026, 2, 100
            )
            RETURNING id
            """
        ),
        {
            "run_key": f"quarterly-run-{token}",
            "snapshot_set_id": snapshot_set_id,
            "rule_version_id": rule_version_id,
        },
    ).scalar_one()


def _new_run(engine: Engine, seed: QuarterlySeed) -> UUID:
    with engine.begin() as connection:
        return _insert_run(
            connection,
            token=uuid4().hex,
            snapshot_set_id=seed.snapshot_set_id,
            rule_version_id=seed.rule_version_id,
        )


def _new_snapshot_run(
    engine: Engine,
    seed: QuarterlySeed,
    *,
    values: Mapping[str, Decimal],
) -> tuple[UUID, UUID]:
    token = uuid4().hex
    target_values = DEFAULT_VALUES | dict(values)
    with engine.begin() as connection:
        frozen = connection.execute(
            text("SELECT lineage FROM accounting_snapshot WHERE id = :snapshot_id"),
            {"snapshot_id": seed.snapshot_id},
        ).scalar_one()
        amounts = {metric["metric_code"]: metric for metric in frozen["metrics"]}
        for metric_code, amount in target_values.items():
            amounts[metric_code]["amount"] = format(amount, "f")
        frozen["sources"] = [{"source": "SAP", "version": token}]
        snapshot_id = connection.execute(
            text(
                """
                INSERT INTO accounting_snapshot (
                    company_id, tax_master_version_id, period,
                    source_version_set_hash, status, currency, amount_scale,
                    record_count, control_total, checksum, lineage, published_at
                )
                VALUES (
                    :company_id, :master_id, :period, :source_hash,
                    'PUBLISHED', 'CNY', 2, :record_count, :control_total,
                    :checksum, CAST(:lineage AS jsonb), now()
                )
                RETURNING id
                """
            ),
            {
                "company_id": seed.company_id,
                "master_id": seed.tax_master_version_id,
                "period": PERIOD,
                "source_hash": _digest(f"snapshot-sources:{token}"),
                "record_count": len(METRICS),
                "control_total": sum(target_values.values(), Decimal("0")),
                "checksum": _digest(f"snapshot:{token}"),
                "lineage": json.dumps(frozen),
            },
        ).scalar_one()
        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:set_key, :period, 'DRAFT', 100)
                RETURNING id
                """
            ),
            {"set_key": f"quarterly-set-{token}", "period": PERIOD},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                VALUES (:set_id, :company_id, :snapshot_id)
                """
            ),
            {
                "set_id": snapshot_set_id,
                "company_id": seed.company_id,
                "snapshot_id": snapshot_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                SELECT :new_set_id, company_id, snapshot_id
                FROM snapshot_set_member
                WHERE snapshot_set_id = :old_set_id
                  AND company_id <> :target_company_id
                """
            ),
            {
                "new_set_id": snapshot_set_id,
                "old_set_id": seed.snapshot_set_id,
                "target_company_id": seed.company_id,
            },
        )
        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :set_id"),
            {"set_id": snapshot_set_id},
        )
        run_id = _insert_run(
            connection,
            token=token,
            snapshot_set_id=snapshot_set_id,
            rule_version_id=seed.rule_version_id,
        )
    return run_id, snapshot_id


def _rows(engine: Engine, table: str, run_id: UUID) -> list[dict[str, object]]:
    with engine.connect() as connection:
        result = connection.execute(
            text(f"SELECT * FROM {table} WHERE run_id = :run_id ORDER BY monitor_type"),
            {"run_id": run_id},
        )
        return [dict(row) for row in result.mappings()]


def test_all_alerts_persist_three_exact_detections_and_isolated_cases(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)

    result = QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    assert result.replayed is False
    assert len(result.detection_ids) == 3
    assert len(result.case_ids) == 3
    detections = _rows(engine, "detection_record", seed.run_id)
    assert [row["monitor_type"] for row in detections] == [
        "ACCRUAL_ACCURACY",
        "TAX_BURDEN",
        "POTENTIAL_TAX_COST",
    ]
    assert [row["alert_code"] for row in detections] == [
        "UNDER_ACCRUED",
        "TAX_BURDEN_HIGH",
        "POTENTIAL_TAX_COST",
    ]
    burden = next(row for row in detections if row["monitor_type"] == "TAX_BURDEN")
    assert burden["result_amount"] is None
    assert burden["tax_burden_rate"] == Decimal("2.500000000000")
    assert burden["tax_burden_deviation"] == Decimal("2.420000000000")
    assert burden["rate_value"] == Decimal("0.250000000000")
    assert burden["formula_substitution"]["current_tax_burden"] == "2.50"
    assert burden["lineage"]["snapshot"]["id"] == str(seed.snapshot_id)
    assert burden["lineage"]["rule_version"]["id"] == str(seed.rule_version_id)
    assert burden["lineage"]["tax_master_version"]["id"] == str(seed.tax_master_version_id)
    assert "source_file_name" not in burden["lineage"]["tax_master_version"]
    assert "imported_at" not in burden["lineage"]["tax_master_version"]
    assert burden["lineage"]["sources"]

    with engine.connect() as connection:
        cases = list(
            connection.execute(
                text(
                    """
                    SELECT * FROM risk_case
                    WHERE company_id = :company_id
                    ORDER BY monitor_type
                    """
                ),
                {"company_id": seed.company_id},
            ).mappings()
        )
    assert len({row["fingerprint"] for row in cases}) == 3
    burden_case = next(row for row in cases if row["monitor_type"] == "TAX_BURDEN")
    assert burden_case["risk_amount"] is None
    assert burden_case["risk_rate"] == Decimal("2.420000000000")
    assert burden_case["risk_direction"] == "HIGH"
    for row in cases:
        if row["monitor_type"] != "TAX_BURDEN":
            assert row["risk_amount"] is not None
            assert row["risk_rate"] is None


def test_detection_lineage_uses_new_metadata_from_frozen_snapshot(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine, include_source_metadata=True)

    QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    detection = _rows(engine, "detection_record", seed.run_id)[0]
    master = detection["lineage"]["tax_master_version"]
    assert master["source_file_name"] == "tax-master.xlsx"
    assert master["imported_at"] == "2026-07-01T09:45:00Z"
    source_batch = detection["lineage"]["sources"][0]["batch"]
    assert source_batch["extraction_time"] == "2026-07-01T08:15:30Z"
    assert source_batch["payload_ref"] == "sap-quarterly-2026-q2.csv"


def test_run_rejects_new_frozen_master_source_file_drift(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine, include_source_metadata=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE tax_master_version
                SET source_file_name = 'mutable-display-name.xlsx'
                WHERE id = :id
                """
            ),
            {"id": seed.tax_master_version_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "FROZEN_MASTER_MISMATCH"
    assert _rows(engine, "detection_record", seed.run_id) == []


def test_run_rejects_new_frozen_master_import_timestamp_drift(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine, include_source_metadata=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE tax_master_version
                SET created_at = TIMESTAMPTZ '2026-07-01 09:46:00+00'
                WHERE id = :id
                """
            ),
            {"id": seed.tax_master_version_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "FROZEN_MASTER_MISMATCH"
    assert _rows(engine, "detection_record", seed.run_id) == []


def test_run_rejects_naive_frozen_master_import_timestamp(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(
        engine,
        include_source_metadata=True,
        lineage_imported_at="2026-07-01T09:45:00",
    )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "FROZEN_MASTER_MISMATCH"
    assert _rows(engine, "detection_record", seed.run_id) == []


def test_run_rejects_naive_frozen_source_extraction_timestamp(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(
        engine,
        include_source_metadata=True,
        lineage_extraction_time="2026-07-01T08:15:30",
    )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "SNAPSHOT_LINEAGE_INVALID"
    assert _rows(engine, "detection_record", seed.run_id) == []


@pytest.mark.parametrize(
    ("values", "average", "expected"),
    [
        (
            {
                "current_quarter_current_tax": Decimal("250"),
                "cumulative_revenue": Decimal("3125"),
                "other_payables_accrual": Decimal("0"),
            },
            Decimal("0.08"),
            set(),
        ),
        (
            {
                "cumulative_profit": Decimal("120"),
                "current_quarter_current_tax": Decimal("30"),
                "cumulative_revenue": Decimal("1000"),
                "other_payables_accrual": Decimal("0"),
            },
            Decimal("0.08"),
            {"TAX_BURDEN_LOW"},
        ),
        (
            {
                "cumulative_profit": Decimal("520"),
                "current_quarter_current_tax": Decimal("130"),
                "cumulative_revenue": Decimal("1000"),
                "other_payables_accrual": Decimal("0"),
            },
            Decimal("0.08"),
            {"TAX_BURDEN_HIGH"},
        ),
        (
            {
                "current_quarter_current_tax": Decimal("300"),
                "cumulative_revenue": Decimal("3125"),
                "other_payables_accrual": Decimal("0"),
            },
            Decimal("0.08"),
            {"OVER_ACCRUED"},
        ),
        (
            {
                "current_quarter_current_tax": Decimal("250"),
                "cumulative_revenue": Decimal("3125"),
                "other_payables_accrual": Decimal("-100"),
            },
            Decimal("0.08"),
            {"POTENTIAL_TAX_COST"},
        ),
        (
            {
                "current_quarter_current_tax": Decimal("250"),
                "cumulative_revenue": Decimal("0"),
                "other_payables_accrual": Decimal("0"),
            },
            Decimal("0.08"),
            set(),
        ),
    ],
    ids=(
        "no-alert",
        "exact-low",
        "exact-high",
        "accrual-only",
        "potential-only",
        "burden-uncalculable",
    ),
)
def test_monitor_alerts_create_only_their_own_cases(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    values: Mapping[str, Decimal],
    average: Decimal,
    expected: set[str],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine, values=values, average_tax_burden=average)

    QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    detections = _rows(engine, "detection_record", seed.run_id)
    assert len(detections) == 3
    assert {row["alert_code"] for row in detections if row["alert_code"]} == expected
    if values.get("cumulative_revenue") == 0:
        burden = next(row for row in detections if row["monitor_type"] == "TAX_BURDEN")
        assert burden["calculation_status"] == "NOT_CALCULABLE"
        assert burden["not_calculated_reason"] == "REVENUE_NON_POSITIVE"
    with engine.connect() as connection:
        actual_cases = set(
            connection.execute(
                text(
                    "SELECT alert_code FROM detection_record "
                    "WHERE run_id = :run_id AND id IN "
                    "(SELECT latest_detection_id FROM risk_case)"
                ),
                {"run_id": seed.run_id},
            ).scalars()
        )
    assert actual_cases == expected


def test_same_run_is_idempotent_and_new_run_reuses_cases_without_resetting_status(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)

    first = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    replay = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    assert replay.replayed is True
    assert replay.detection_ids == first.detection_ids
    assert replay.case_ids == first.case_ids
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE risk_case SET status = 'ASSIGNED' WHERE company_id = :company_id"),
            {"company_id": seed.company_id},
        )
    second_run_id = _new_run(engine, seed)

    second = service.execute(run_id=second_run_id, snapshot_id=seed.snapshot_id)

    assert len(second.detection_ids) == 3
    assert second.case_ids == first.case_ids
    with engine.connect() as connection:
        detection_count = connection.execute(
            text("SELECT count(*) FROM detection_record WHERE company_id = :company_id"),
            {"company_id": seed.company_id},
        ).scalar_one()
        case_rows = list(
            connection.execute(
                text(
                    "SELECT status, row_version, latest_detection_id "
                    "FROM risk_case WHERE company_id = :company_id"
                ),
                {"company_id": seed.company_id},
            ).mappings()
        )
    assert detection_count == 6
    assert len(case_rows) == 3
    assert {row["status"] for row in case_rows} == {"ASSIGNED"}
    assert {row["row_version"] for row in case_rows} == {2}
    assert {row["latest_detection_id"] for row in case_rows} == set(second.detection_ids)


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"])
def test_complete_detection_set_replays_after_run_reaches_a_terminal_status(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    status: str,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    first = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE monitoring_run SET status = :status WHERE id = :run_id"),
            {"status": status, "run_id": seed.run_id},
        )

    replay = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert replay.replayed is True
    assert replay.detection_ids == first.detection_ids
    assert replay.case_ids == first.case_ids


@pytest.mark.parametrize("current_drift", ["master", "company"])
def test_terminal_replay_uses_frozen_evidence_after_current_governance_drift(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    current_drift: str,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    first = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE monitoring_run SET status = 'SUCCEEDED' WHERE id = :run_id"),
            {"run_id": seed.run_id},
        )
        if current_drift == "master":
            connection.execute(
                text("UPDATE tax_master_version SET tax_rate = 0.20 WHERE id = :master_id"),
                {"master_id": seed.tax_master_version_id},
            )
        else:
            connection.execute(
                text(
                    "UPDATE company SET lifecycle = 'INACTIVE', deactivated_at = now() "
                    "WHERE id = :company_id"
                ),
                {"company_id": seed.company_id},
            )

    replay = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert replay.replayed is True
    assert replay.detection_ids == first.detection_ids
    assert replay.case_ids == first.case_ids


def test_terminal_replay_rejects_a_run_repointed_to_another_rule(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        other_rule_id = connection.execute(
            text(
                """
                INSERT INTO rule_version (
                    rule_code, version, status, effective_from, definition,
                    change_reason, published_at, approved_by
                ) VALUES (
                    'OTHER_RULE', :version, 'DRAFT', '2026-01-01', '{}'::jsonb,
                    'identity mismatch test', NULL, NULL
                ) RETURNING id
                """
            ),
            {"version": uuid4().hex},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE monitoring_run SET status = 'SUCCEEDED', "
                "rule_version_id = :rule_id WHERE id = :run_id"
            ),
            {"rule_id": other_rule_id, "run_id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert caught.value.error_code == "DETECTION_IDENTITY_MISMATCH"


def test_terminal_replay_rejects_snapshot_removed_from_the_frozen_set(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE snapshot_set_member DISABLE TRIGGER trg_snapshot_set_member_immutable"
            )
        )
        connection.execute(
            text(
                "DELETE FROM snapshot_set_member "
                "WHERE snapshot_set_id = :set_id AND snapshot_id = :snapshot_id"
            ),
            {"set_id": seed.snapshot_set_id, "snapshot_id": seed.snapshot_id},
        )
        connection.execute(
            text("ALTER TABLE snapshot_set_member ENABLE TRIGGER trg_snapshot_set_member_immutable")
        )
        connection.execute(
            text("UPDATE monitoring_run SET status = 'SUCCEEDED' WHERE id = :run_id"),
            {"run_id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert caught.value.error_code == "SNAPSHOT_NOT_IN_RUN_SET"


def test_pending_run_cannot_replay_an_existing_detection_set(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE monitoring_run SET status = 'PENDING' WHERE id = :run_id"),
            {"run_id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert caught.value.error_code == "MONITORING_RUN_NOT_RUNNING"


def test_replay_rejects_detection_evidence_from_a_different_identity(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE detection_record DISABLE TRIGGER trg_detection_record_immutable")
        )
        connection.execute(
            text(
                "UPDATE detection_record SET currency = 'USD' "
                "WHERE run_id = :run_id AND monitor_type = 'TAX_BURDEN'"
            ),
            {"run_id": seed.run_id},
        )
        connection.execute(
            text("ALTER TABLE detection_record ENABLE TRIGGER trg_detection_record_immutable")
        )
        connection.execute(
            text("UPDATE monitoring_run SET status = 'SUCCEEDED' WHERE id = :run_id"),
            {"run_id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    assert caught.value.error_code == "DETECTION_IDENTITY_MISMATCH"


def test_newer_run_refreshes_case_summary_without_resetting_workflow(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    service = QuarterlyRunService(uow_factory)
    first = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE risk_case SET status = 'ASSIGNED', assignee = 'case-owner' "
                "WHERE company_id = :company_id"
            ),
            {"company_id": seed.company_id},
        )
    new_run_id, new_snapshot_id = _new_snapshot_run(
        engine,
        seed,
        values={
            "cumulative_profit": Decimal("120"),
            "current_quarter_current_tax": Decimal("50"),
            "cumulative_revenue": Decimal("1000"),
            "other_payables_accrual": Decimal("-100"),
        },
    )

    second = service.execute(run_id=new_run_id, snapshot_id=new_snapshot_id)

    assert second.case_ids == first.case_ids
    with engine.connect() as connection:
        cases = list(
            connection.execute(
                text(
                    """
                    SELECT risk_case.monitor_type, risk_case.status, risk_case.assignee,
                           risk_case.risk_amount, risk_case.risk_rate,
                           risk_case.risk_direction, risk_case.currency,
                           risk_case.amount_scale, risk_case.lineage,
                           risk_case.latest_detection_id, risk_case.row_version
                    FROM risk_case
                    WHERE risk_case.company_id = :company_id
                    """
                ),
                {"company_id": seed.company_id},
            ).mappings()
        )
    by_monitor = {row["monitor_type"]: row for row in cases}
    assert set(by_monitor) == {
        "ACCRUAL_ACCURACY",
        "TAX_BURDEN",
        "POTENTIAL_TAX_COST",
    }
    assert by_monitor["ACCRUAL_ACCURACY"]["risk_amount"] == Decimal("20.000000000000")
    assert by_monitor["ACCRUAL_ACCURACY"]["risk_direction"] == "OVER"
    assert by_monitor["TAX_BURDEN"]["risk_rate"] == Decimal("0.050000000000")
    assert by_monitor["TAX_BURDEN"]["risk_direction"] == "LOW"
    assert by_monitor["POTENTIAL_TAX_COST"]["risk_amount"] == Decimal("25.000000000000")
    assert by_monitor["POTENTIAL_TAX_COST"]["risk_direction"] == "DECREASE"
    assert {row["status"] for row in cases} == {"ASSIGNED"}
    assert {row["assignee"] for row in cases} == {"case-owner"}
    assert {row["currency"] for row in cases} == {"CNY"}
    assert {row["amount_scale"] for row in cases} == {2}
    assert {row["latest_detection_id"] for row in cases} == set(second.detection_ids)
    assert {row["row_version"] for row in cases} == {2}
    assert {row["lineage"]["snapshot"]["id"] for row in cases} == {str(new_snapshot_id)}


def test_older_run_arriving_late_cannot_overwrite_a_newer_case_summary(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    new_run_id, new_snapshot_id = _new_snapshot_run(
        engine,
        seed,
        values={
            "cumulative_profit": Decimal("120"),
            "current_quarter_current_tax": Decimal("50"),
            "cumulative_revenue": Decimal("1000"),
            "other_payables_accrual": Decimal("-100"),
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE monitoring_run SET created_at = '2026-01-01 00:00:00+00' WHERE id = :run_id"
            ),
            {"run_id": seed.run_id},
        )
        connection.execute(
            text(
                "UPDATE monitoring_run SET created_at = '2026-01-02 00:00:00+00' WHERE id = :run_id"
            ),
            {"run_id": new_run_id},
        )
    service = QuarterlyRunService(uow_factory)
    newer = service.execute(run_id=new_run_id, snapshot_id=new_snapshot_id)

    with engine.connect() as connection:
        before = list(
            connection.execute(
                text(
                    """
                    SELECT monitor_type, latest_detection_id, risk_amount, risk_rate,
                           risk_direction, lineage, row_version
                    FROM risk_case WHERE company_id = :company_id
                    ORDER BY monitor_type
                    """
                ),
                {"company_id": seed.company_id},
            ).mappings()
        )
    older = service.execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)
    with engine.connect() as connection:
        after = list(
            connection.execute(
                text(
                    """
                    SELECT monitor_type, latest_detection_id, risk_amount, risk_rate,
                           risk_direction, lineage, row_version
                    FROM risk_case WHERE company_id = :company_id
                    ORDER BY monitor_type
                    """
                ),
                {"company_id": seed.company_id},
            ).mappings()
        )

    assert older.case_ids == newer.case_ids
    assert [dict(row) for row in after] == [dict(row) for row in before]
    assert {row["latest_detection_id"] for row in after} == set(newer.detection_ids)
    assert {row["row_version"] for row in after} == {1}


def test_failure_rolls_back_all_detections_and_cases(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)

    def fail_after_first_detection(stage: str) -> None:
        if stage == "detection_persisted":
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        QuarterlyRunService(
            uow_factory,
            failure_injector=fail_after_first_detection,
        ).execute(run_id=seed.run_id, snapshot_id=seed.snapshot_id)

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM detection_record WHERE run_id = :run_id"),
                {"run_id": seed.run_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM risk_case WHERE company_id = :company_id"),
                {"company_id": seed.company_id},
            ).scalar_one()
            == 0
        )


def test_concurrent_same_run_serializes_to_one_detection_set(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)

    def execute_once():
        return QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute_once(), range(2)))

    assert sum(not result.replayed for result in results) == 1
    assert {result.detection_ids for result in results} == {results[0].detection_ids}
    assert len(_rows(engine, "detection_record", seed.run_id)) == 3
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM risk_case WHERE company_id = :company_id"),
                {"company_id": seed.company_id},
            ).scalar_one()
            == 3
        )


def test_same_run_different_snapshots_execute_concurrently(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    barrier = Barrier(2)

    def execute_once(snapshot_id: UUID):
        reached_first_detection = False

        def synchronize(stage: str) -> None:
            nonlocal reached_first_detection
            if stage == "detection_persisted" and not reached_first_detection:
                reached_first_detection = True
                barrier.wait(timeout=5)

        return QuarterlyRunService(
            uow_factory,
            failure_injector=synchronize,
        ).execute(run_id=seed.run_id, snapshot_id=snapshot_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(execute_once, seed.snapshot_id),
            pool.submit(execute_once, seed.secondary_snapshot_id),
        ]
        results = [future.result() for future in futures]

    assert all(result.replayed is False for result in results)
    assert len(_rows(engine, "detection_record", seed.run_id)) == 6


def test_detection_records_are_database_immutable(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    result = QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    with pytest.raises(DBAPIError, match="immutable_detection_record"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE detection_record SET direction = 'TAMPERED' WHERE id = :id"),
                {"id": result.detection_ids[0]},
            )
    with pytest.raises(DBAPIError, match="immutable_detection_record"):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM detection_record WHERE id = :id"),
                {"id": result.detection_ids[0]},
            )


def test_migration_seeds_reviewed_formula_manifest_with_valid_sha256(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    _, engine = resources
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                """
                SELECT definition FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1'
                  AND status = 'PUBLISHED'
                ORDER BY effective_from DESC, id
                LIMIT 1
                """
            )
        ).scalar_one()

    manifest = definition["formula_manifest"]
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert definition["formula_manifest_sha256"] == sha256(canonical).hexdigest()
    assert definition["review_status"] == "REVIEWED"
    assert definition["formula_manifest"] == quarterly_runs_application.APPROVED_QUARTERLY_MANIFEST
    assert (
        definition["formula_manifest_sha256"]
        == quarterly_runs_application.APPROVED_QUARTERLY_MANIFEST_SHA256
    )


@pytest.mark.parametrize("recompute_hash", [False, True], ids=("stale-hash", "forged-hash"))
def test_run_rejects_any_rule_manifest_outside_the_fixed_reviewed_definition(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    recompute_hash: bool,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    with engine.begin() as connection:
        definition = connection.execute(
            text("SELECT definition FROM rule_version WHERE id = :id"),
            {"id": seed.rule_version_id},
        ).scalar_one()
        definition["formula_manifest"]["formulas"]["cumulative_tax_payable"] = "cumulative_base*0"
        if recompute_hash:
            canonical = json.dumps(
                definition["formula_manifest"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            definition["formula_manifest_sha256"] = sha256(canonical).hexdigest()
        forged_rule_id = connection.execute(
            text(
                """
                INSERT INTO rule_version (
                    rule_code, version, status, effective_from, definition,
                    change_reason, published_at, approved_by
                )
                VALUES (
                    'QUARTERLY_V1', :version, 'PUBLISHED', '2026-01-01',
                    CAST(:definition AS jsonb), 'forged test rule', now(), 'forger'
                )
                RETURNING id
                """
            ),
            {
                "version": f"forged-{uuid4().hex}",
                "definition": json.dumps(definition),
            },
        ).scalar_one()
        connection.execute(
            text("UPDATE monitoring_run SET rule_version_id = :rule_id WHERE id = :run_id"),
            {"rule_id": forged_rule_id, "run_id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "QUARTERLY_RULE_MANIFEST_INVALID"
    assert _rows(engine, "detection_record", seed.run_id) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("tax_rate", Decimal("0.20")),
        ("loss_carryforward", Decimal("1")),
        ("average_tax_burden_rate_3y", Decimal("0.09")),
    ],
)
def test_run_rejects_current_master_drift_from_frozen_snapshot_lineage(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    column: str,
    value: Decimal,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE tax_master_version SET {column} = :value WHERE id = :id"),
            {"value": value, "id": seed.tax_master_version_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "FROZEN_MASTER_MISMATCH"
    assert _rows(engine, "detection_record", seed.run_id) == []


@pytest.mark.parametrize(
    "status",
    ["PENDING", "SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"],
)
def test_run_executes_only_while_status_is_running(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    status: str,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE monitoring_run SET status = :status WHERE id = :id"),
            {"status": status, "id": seed.run_id},
        )

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=seed.run_id,
            snapshot_id=seed.snapshot_id,
        )

    assert caught.value.error_code == "MONITORING_RUN_NOT_RUNNING"


def test_rate_storage_rounding_is_independent_of_ambient_decimal_context() -> None:
    ambient = getcontext()
    previous_rounding = ambient.rounding
    ambient.rounding = ROUND_DOWN
    try:
        stored = quarterly_runs_application._database_decimal(Decimal("1.2345678901235"))
    finally:
        ambient.rounding = previous_rounding

    assert stored == Decimal("1.234567890124")


def test_tax_burden_rate_overflow_fails_only_that_monitor(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(
        engine,
        values={
            "cumulative_profit": Decimal("99999999999999999999999999"),
            "cumulative_revenue": Decimal("0.000000000001"),
            "current_quarter_current_tax": Decimal("0"),
            "other_payables_accrual": Decimal("0"),
            "hesi_no_invoice": Decimal("0"),
        },
    )

    result = QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    assert len(result.detection_ids) == 3
    detections = _rows(engine, "detection_record", seed.run_id)
    burden = next(row for row in detections if row["monitor_type"] == "TAX_BURDEN")
    assert burden["calculation_status"] == "FAILED"
    assert burden["not_calculated_reason"] == "RATE_VALUE_OVERFLOW"
    assert burden["tax_burden_rate"] is None
    assert burden["tax_burden_deviation"] is None
    assert {
        row["calculation_status"] for row in detections if row["monitor_type"] != "TAX_BURDEN"
    } == {"CALCULATED"}
    with engine.connect() as connection:
        cases = list(
            connection.execute(
                text("SELECT monitor_type FROM risk_case WHERE company_id = :company_id"),
                {"company_id": seed.company_id},
            ).scalars()
        )
    assert cases == ["ACCRUAL_ACCURACY"]


@pytest.mark.parametrize(
    ("values", "monitor_type", "field", "expected_value", "direction"),
    [
        (
            {
                "cumulative_profit": Decimal("120"),
                "current_quarter_current_tax": Decimal("30"),
                "cumulative_revenue": Decimal("1000"),
                "other_payables_accrual": Decimal("0"),
            },
            "TAX_BURDEN",
            "risk_rate",
            Decimal("0.050000000000"),
            "LOW",
        ),
        (
            {
                "current_quarter_current_tax": Decimal("300"),
                "cumulative_revenue": Decimal("3125"),
                "other_payables_accrual": Decimal("0"),
            },
            "ACCRUAL_ACCURACY",
            "risk_amount",
            Decimal("50.000000000000"),
            "OVER",
        ),
        (
            {
                "current_quarter_current_tax": Decimal("250"),
                "cumulative_revenue": Decimal("3125"),
                "other_payables_accrual": Decimal("-100"),
            },
            "POTENTIAL_TAX_COST",
            "risk_amount",
            Decimal("25.000000000000"),
            "DECREASE",
        ),
    ],
    ids=("burden-low", "over-accrual", "negative-potential"),
)
def test_case_values_are_nonnegative_while_direction_preserves_the_sign(
    resources: tuple[Callable[[], UnitOfWork], Engine],
    values: Mapping[str, Decimal],
    monitor_type: str,
    field: str,
    expected_value: Decimal,
    direction: str,
) -> None:
    uow_factory, engine = resources
    seed = _seed_quarterly_case(engine, values=values)

    QuarterlyRunService(uow_factory).execute(
        run_id=seed.run_id,
        snapshot_id=seed.snapshot_id,
    )

    with engine.connect() as connection:
        risk_case = (
            connection.execute(
                text(
                    "SELECT risk_amount, risk_rate, risk_direction FROM risk_case "
                    "WHERE company_id = :company_id AND monitor_type = :monitor_type"
                ),
                {"company_id": seed.company_id, "monitor_type": monitor_type},
            )
            .mappings()
            .one()
        )
    assert risk_case[field] == expected_value
    assert risk_case["risk_direction"] == direction


def test_run_rejects_snapshot_outside_the_frozen_set(
    resources: tuple[Callable[[], UnitOfWork], Engine],
) -> None:
    uow_factory, engine = resources
    first = _seed_quarterly_case(engine)
    outsider = _seed_quarterly_case(engine)

    with pytest.raises(QuarterlyRunError) as caught:
        QuarterlyRunService(uow_factory).execute(
            run_id=first.run_id,
            snapshot_id=outsider.snapshot_id,
        )

    assert caught.value.error_code == "SNAPSHOT_NOT_IN_RUN_SET"
    assert _rows(engine, "detection_record", first.run_id) == []

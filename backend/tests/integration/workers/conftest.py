from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from hashlib import sha256
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


PERIOD = date(2026, 6, 30)
METRICS: dict[str, Decimal] = {
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
class QuarterlyBatch105Seed:
    snapshot_set_id: UUID
    rule_version_id: UUID
    company_ids: tuple[UUID, ...]
    snapshot_ids: tuple[UUID, ...]
    inactive_company_id: UUID
    failed_snapshot_id: UUID


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _snapshot_lineage(
    *,
    token: str,
    company_index: int,
    master_id: UUID,
    master_batch_id: UUID,
    master_version: str,
    master_checksum: str,
    source_row_number: int,
) -> dict[str, object]:
    return {
        "schema_version": "quarterly-v1",
        "metrics": [
            {
                "metric_code": metric_code,
                "amount": format(amount, "f"),
                "source_record": {
                    "source_record_key": f"{token}:{company_index}:{metric_code}",
                    "batch_id": str(master_batch_id),
                    "lineage": {"row_number": metric_index + 2, "worksheet": "Q2"},
                },
            }
            for metric_index, (metric_code, amount) in enumerate(METRICS.items())
        ],
        "sources": [
            {
                "source": "SAP",
                "source_version": f"sap-{token}-{company_index}",
            }
        ],
        "tax_master": {
            "id": str(master_id),
            "version": master_version,
            "source_batch_id": str(master_batch_id),
            "source_checksum": master_checksum,
            "source_row_number": source_row_number,
            "valid_from": "2026-01-01",
            "valid_to": None,
            "tax_rate": "0.25",
            "loss_carryforward": "0",
            "three_year_average_tax_burden": "0.08",
            "currency": "CNY",
            "amount_scale": 2,
        },
    }


def _seed_105_company_snapshot_set(engine: Engine) -> QuarterlyBatch105Seed:
    token = uuid4().hex
    company_ids: list[UUID] = []
    snapshot_ids: list[UUID] = []
    with engine.begin() as connection:
        rule_version_id = connection.execute(
            text(
                """
                SELECT id
                FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1'
                  AND version = 'phase-1-reviewed'
                  AND status = 'PUBLISHED'
                """
            )
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
                    105, 105, 0, 0, :checksum
                )
                RETURNING id
                """
            ),
            {
                "batch_key": f"quarterly-batch-master-{token}",
                "period": PERIOD,
                "checksum": _digest(f"master-batch:{token}"),
            },
        ).scalar_one()
        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:set_key, :period, 'DRAFT', 105)
                RETURNING id
                """
            ),
            {"set_key": f"quarterly-batch-set-{token}", "period": PERIOD},
        ).scalar_one()

        for index in range(105):
            company_id = connection.execute(
                text(
                    """
                    INSERT INTO company (company_code, company_name, lifecycle)
                    VALUES (:code, :name, 'ACTIVE')
                    RETURNING id
                    """
                ),
                {
                    "code": f"QB-{token}-{index:03d}",
                    "name": f"Quarterly batch company {index:03d}",
                },
            ).scalar_one()
            master_version = f"v-{token}-{index:03d}"
            master_checksum = _digest(f"master-row:{token}:{index}")
            source_row_number = index + 2
            master_id = connection.execute(
                text(
                    """
                    INSERT INTO tax_master_version (
                        company_id, source_batch_id, valid_from, version, status,
                        tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                        currency, amount_scale, source_file_name, source_checksum,
                        source_row_number, uploaded_by, data, published_at, approved_by
                    )
                    VALUES (
                        :company_id, :source_batch_id, DATE '2026-01-01', :version,
                        'PUBLISHED', 0.25, 0, 0.08, 'CNY', 2,
                        'tax-master.xlsx', :checksum, :row_number, 'maker',
                        '{}'::jsonb, now(), 'reviewer'
                    )
                    RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "source_batch_id": master_batch_id,
                    "version": master_version,
                    "checksum": master_checksum,
                    "row_number": source_row_number,
                },
            ).scalar_one()
            lineage = _snapshot_lineage(
                token=token,
                company_index=index,
                master_id=master_id,
                master_batch_id=master_batch_id,
                master_version=master_version,
                master_checksum=master_checksum,
                source_row_number=source_row_number,
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
                    "record_count": len(METRICS),
                    "control_total": sum(METRICS.values(), Decimal("0")),
                    "checksum": _digest(f"snapshot:{token}:{index}"),
                    "lineage": json.dumps(lineage, sort_keys=True),
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
            company_ids.append(company_id)
            snapshot_ids.append(snapshot_id)

        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :set_id"),
            {"set_id": snapshot_set_id},
        )
        connection.execute(
            text(
                """
                UPDATE company
                SET lifecycle = 'INACTIVE',
                    deactivated_at = now(),
                    lifecycle_reason = 'runtime batch isolation test'
                WHERE id = :company_id
                """
            ),
            {"company_id": company_ids[103]},
        )

    return QuarterlyBatch105Seed(
        snapshot_set_id=snapshot_set_id,
        rule_version_id=rule_version_id,
        company_ids=tuple(company_ids),
        snapshot_ids=tuple(snapshot_ids),
        inactive_company_id=company_ids[103],
        failed_snapshot_id=snapshot_ids[104],
    )


def _cleanup_seed(engine: Engine, seed: QuarterlyBatch105Seed) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM risk_case WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(
            text("ALTER TABLE detection_record DISABLE TRIGGER trg_detection_record_immutable")
        )
        connection.execute(
            text("DELETE FROM detection_record WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(
            text("ALTER TABLE detection_record ENABLE TRIGGER trg_detection_record_immutable")
        )
        if connection.execute(
            text("SELECT to_regclass('monitoring_run_company')")
        ).scalar_one_or_none() is not None:
            connection.execute(
                text(
                    """
                    DELETE FROM monitoring_run_company
                    WHERE run_id IN (
                        SELECT id FROM monitoring_run WHERE snapshot_set_id = :set_id
                    )
                    """
                ),
                {"set_id": seed.snapshot_set_id},
            )
        connection.execute(
            text("DELETE FROM monitoring_run WHERE snapshot_set_id = :set_id"),
            {"set_id": seed.snapshot_set_id},
        )
        connection.execute(
            text("ALTER TABLE snapshot_set_member DISABLE TRIGGER USER")
        )
        connection.execute(
            text("DELETE FROM snapshot_set_member WHERE snapshot_set_id = :set_id"),
            {"set_id": seed.snapshot_set_id},
        )
        connection.execute(text("ALTER TABLE snapshot_set_member ENABLE TRIGGER USER"))
        connection.execute(text("ALTER TABLE snapshot_set DISABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM snapshot_set WHERE id = :set_id"),
            {"set_id": seed.snapshot_set_id},
        )
        connection.execute(text("ALTER TABLE snapshot_set ENABLE TRIGGER USER"))
        connection.execute(text("ALTER TABLE accounting_snapshot DISABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM accounting_snapshot WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(text("ALTER TABLE accounting_snapshot ENABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM tax_master_version WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(
            text(
                """
                DELETE FROM ingest_batch
                WHERE source_batch_key LIKE 'quarterly-batch-master-%'
                  AND NOT EXISTS (
                      SELECT 1 FROM tax_master_version
                      WHERE tax_master_version.source_batch_id = ingest_batch.id
                  )
                """
            )
        )
        connection.execute(
            text("DELETE FROM company WHERE id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )


@pytest.fixture
def quarterly_batch_resources(
    isolated_database_url: str,
) -> Iterator[tuple[Callable[[], UnitOfWork], Engine, QuarterlyBatch105Seed]]:
    database_engine, factory = create_session_factory(isolated_database_url)
    seed = _seed_105_company_snapshot_set(database_engine)
    try:
        yield partial(UnitOfWork, factory), database_engine, seed
    finally:
        _cleanup_seed(database_engine, seed)
        database_engine.dispose()

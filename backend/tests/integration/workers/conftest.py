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

from tax_risk.application.snapshots import ExpectedSnapshotMember, SnapshotService
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
    sap_batch_ids: tuple[UUID, ...]
    master_batch_id: UUID
    inactive_company_id: UUID
    failed_snapshot_id: UUID


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _seed_105_company_snapshot_set(
    engine: Engine,
    uow_factory: Callable[[], UnitOfWork],
) -> QuarterlyBatch105Seed:
    token = uuid4().hex
    company_ids: list[UUID] = []
    company_codes: list[str] = []
    sap_batch_ids: list[UUID] = []
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

        for index in range(105):
            company_code = f"QB-{token}-{index:03d}"
            company_id = connection.execute(
                text(
                    """
                    INSERT INTO company (company_code, company_name, lifecycle)
                    VALUES (:code, :name, 'ACTIVE')
                    RETURNING id
                    """
                ),
                {
                    "code": company_code,
                    "name": f"Quarterly batch company {index:03d}",
                },
            ).scalar_one()
            master_version = f"v-{token}-{index:03d}"
            master_checksum = _digest(f"master-row:{token}:{index}")
            source_row_number = index + 2
            connection.execute(
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
            sap_batch_id = connection.execute(
                text(
                    """
                    INSERT INTO ingest_batch (
                        source, source_batch_key, dataset_code, status, extraction_time,
                        period, mode, schema_version, currency, amount_scale,
                        record_count, accepted_count, rejected_count, control_total, checksum,
                        payload_ref
                    )
                    VALUES (
                        'SAP', :batch_key, 'quarterly_metric', 'SUCCEEDED', now(),
                        :period, 'FULL', 'quarterly-v1', 'CNY', 2,
                        :record_count, :record_count, 0, :control_total, :checksum,
                        :payload_ref
                    )
                    RETURNING id
                    """
                ),
                {
                    "batch_key": f"quarterly-batch-sap-{token}-{index:03d}",
                    "period": PERIOD,
                    "record_count": len(METRICS),
                    "control_total": sum(METRICS.values(), Decimal("0")),
                    "checksum": _digest(f"sap-batch:{token}:{index}"),
                    "payload_ref": f"sap-quarterly-{token}-{index:03d}.csv",
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    )
                    VALUES (
                        :batch_id, :record_key, :company_id, 'quarterly_metric', :period,
                        'CNY', 2, :amount, CAST(:payload AS jsonb),
                        CAST(:lineage AS jsonb), now()
                    )
                    """
                ),
                [
                    {
                        "batch_id": sap_batch_id,
                        "record_key": f"{token}:{index}:{metric_code}",
                        "company_id": company_id,
                        "period": PERIOD,
                        "amount": amount,
                        "payload": json.dumps(
                            {
                                "company_code": company_code,
                                "period": PERIOD.isoformat(),
                                "currency": "CNY",
                                "amount_scale": 2,
                                "metric_code": metric_code,
                                "amount": format(amount, "f"),
                            },
                            sort_keys=True,
                        ),
                        "lineage": json.dumps(
                            {
                                "row_number": metric_index + 2,
                                "worksheet": "Q2",
                            },
                            sort_keys=True,
                        ),
                    }
                    for metric_index, (metric_code, amount) in enumerate(METRICS.items())
                ],
            )
            company_ids.append(company_id)
            company_codes.append(company_code)
            sap_batch_ids.append(sap_batch_id)

    snapshot_service = SnapshotService(uow_factory)
    snapshot_ids: list[UUID] = []
    expected_members: list[ExpectedSnapshotMember] = []
    for company_id, company_code, sap_batch_id in zip(
        company_ids,
        company_codes,
        sap_batch_ids,
        strict=True,
    ):
        validated = snapshot_service.validate(
            company_code=company_code,
            period=PERIOD,
            source_batch_ids=(sap_batch_id,),
        )
        assert validated.valid, validated.issues
        assert validated.snapshot is not None
        published = snapshot_service.publish(validated.snapshot.id)
        snapshot_ids.append(published.id)
        expected_members.append(
            ExpectedSnapshotMember(company_id=company_id, snapshot_id=published.id)
        )

    published_set = snapshot_service.publish_set(
        set_key=f"quarterly-batch-set-{token}",
        period=PERIOD,
        expected_members=tuple(expected_members),
    )

    with engine.begin() as connection:
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
        snapshot_set_id=published_set.id,
        rule_version_id=rule_version_id,
        company_ids=tuple(company_ids),
        snapshot_ids=tuple(snapshot_ids),
        sap_batch_ids=tuple(sap_batch_ids),
        master_batch_id=master_batch_id,
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
        connection.execute(text("ALTER TABLE snapshot_source DISABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM snapshot_source WHERE snapshot_id = ANY(:snapshot_ids)"),
            {"snapshot_ids": list(seed.snapshot_ids)},
        )
        connection.execute(text("ALTER TABLE snapshot_source ENABLE TRIGGER USER"))
        connection.execute(text("ALTER TABLE accounting_snapshot DISABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM accounting_snapshot WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(text("ALTER TABLE accounting_snapshot ENABLE TRIGGER USER"))
        connection.execute(
            text("DELETE FROM source_record WHERE batch_id = ANY(:batch_ids)"),
            {"batch_ids": list(seed.sap_batch_ids)},
        )
        connection.execute(
            text("DELETE FROM tax_master_version WHERE company_id = ANY(:company_ids)"),
            {"company_ids": list(seed.company_ids)},
        )
        connection.execute(
            text("DELETE FROM ingest_batch WHERE id = ANY(:batch_ids)"),
            {"batch_ids": [*seed.sap_batch_ids, seed.master_batch_id]},
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
    uow_factory = partial(UnitOfWork, factory)
    seed = _seed_105_company_snapshot_set(database_engine, uow_factory)
    try:
        yield uow_factory, database_engine, seed
    finally:
        _cleanup_seed(database_engine, seed)
        database_engine.dispose()

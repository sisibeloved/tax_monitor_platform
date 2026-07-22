from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
import json
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from tax_risk.application.snapshots import (
    ExpectedSnapshotMember,
    REQUIRED_QUARTERLY_METRICS,
    SnapshotConflictError,
    SnapshotQualityError,
    SnapshotRequestError,
    SnapshotService,
    SnapshotSetView,
    SnapshotView,
)
import tax_risk.application.snapshots as snapshots_application
from tax_risk.application.ingest import BatchView, IngestService
from tax_risk.application.master_data import MasterDataConflictError, TaxMasterService
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.persistence.ingest_repositories import IngestRepository
from tax_risk.persistence.snapshot_models import SnapshotSetStatus, SnapshotStatus


PERIOD = date(2026, 6, 30)


@pytest.fixture
def service_resources(
    isolated_database_url: str,
) -> Iterator[tuple[SnapshotService, Engine, sessionmaker[Session]]]:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        yield SnapshotService(partial(UnitOfWork, factory)), engine, factory
    finally:
        engine.dispose()


def _seed_quality_case(
    engine: Engine,
    *,
    metrics: Sequence[str] = REQUIRED_QUARTERLY_METRICS,
    values: Sequence[Decimal] | None = None,
    batch_status: str = "SUCCEEDED",
    rejected_count: int = 0,
    errors: Sequence[dict[str, object]] = (),
    batch_period: date = PERIOD,
    batch_currency: str = "CNY",
    batch_scale: int = 2,
    control_total_delta: Decimal = Decimal("0"),
    company_lifecycle: str = "ACTIVE",
    master_count: int = 1,
    master_valid_from: date = date(2026, 1, 1),
    master_valid_to: date | None = None,
    master_currency: str = "CNY",
    master_scale: int = 2,
    deferred_tax_rate: Decimal | None = Decimal("0.20"),
    payload_overrides: dict[str, object] | None = None,
    row_overrides: dict[str, object] | None = None,
) -> tuple[str, UUID]:
    token = uuid4().hex
    company_code = f"SNAP-{token}"
    amounts = tuple(values or (Decimal(index) for index in range(1, len(metrics) + 1)))
    assert len(amounts) == len(metrics)
    control_total = sum(amounts, Decimal("0")) + control_total_delta
    deactivated_at = "now()" if company_lifecycle == "INACTIVE" else "NULL"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                f"""
                INSERT INTO company (
                    company_code, company_name, lifecycle, deactivated_at
                )
                VALUES (:code, :name, :lifecycle, {deactivated_at})
                RETURNING id
                """
            ),
            {
                "code": company_code,
                "name": f"Company {token}",
                "lifecycle": company_lifecycle,
            },
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                )
                VALUES (
                    'SAP', :batch_key, 'quarterly_metric', :status, now(),
                    :period, 'FULL', 'quarterly-v1', :currency, :scale,
                    :record_count, :accepted_count, :rejected_count,
                    :control_total, :checksum
                )
                RETURNING id
                """
            ),
            {
                "batch_key": f"quarterly-{token}",
                "status": batch_status,
                "period": batch_period,
                "currency": batch_currency,
                "scale": batch_scale,
                "record_count": len(metrics) + rejected_count,
                "accepted_count": len(metrics),
                "rejected_count": rejected_count,
                "control_total": control_total,
                "checksum": token.ljust(64, "a")[:64],
            },
        ).scalar_one()
        for index, (metric, amount) in enumerate(zip(metrics, amounts, strict=True), start=1):
            payload: dict[str, object] = {
                "company_code": company_code,
                "period": batch_period.isoformat(),
                "currency": batch_currency,
                "amount_scale": batch_scale,
                "metric_code": metric,
                "amount": str(amount),
            }
            payload.update(payload_overrides or {})
            row = {
                "period": batch_period,
                "currency": batch_currency,
                "amount_scale": batch_scale,
                "dataset_code": "quarterly_metric",
            }
            row.update(row_overrides or {})
            connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    )
                    VALUES (
                        :batch_id, :record_key, :company_id, :dataset_code, :period,
                        :currency, :amount_scale, :amount, CAST(:payload AS jsonb),
                        CAST(:lineage AS jsonb), now()
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "record_key": f"{metric}-{index}-{token}",
                    "company_id": company_id,
                    "dataset_code": row["dataset_code"],
                    "period": row["period"],
                    "currency": row["currency"],
                    "amount_scale": row["amount_scale"],
                    "amount": amount,
                    "payload": json.dumps(payload),
                    "lineage": json.dumps({"row_number": index + 1, "token": token}),
                },
            )
        for index, details in enumerate(errors, start=1):
            connection.execute(
                text(
                    """
                    INSERT INTO ingest_error (
                        batch_id, row_number, error_code, message, details, retryable
                    )
                    VALUES (
                        :batch_id, :row_number, 'TEST_REJECTION', 'test rejection',
                        CAST(:details AS jsonb), false
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "row_number": len(metrics) + index + 1,
                    "details": json.dumps(details),
                },
            )
        if master_count:
            master_batch_id = connection.execute(
                text(
                    """
                    INSERT INTO ingest_batch (
                        source, source_batch_key, dataset_code, status, extraction_time,
                        period, mode, schema_version, currency, amount_scale, record_count,
                        accepted_count, rejected_count, control_total, checksum
                    )
                    VALUES (
                        'TAX_MASTER_XLSX', :batch_key, 'tax_master', 'SUCCEEDED', now(),
                        :period, 'FULL', 'tax-master-v1', :currency, :scale,
                        :count, :count, 0, 0, :checksum
                    )
                    RETURNING id
                    """
                ),
                {
                    "batch_key": f"master-{token}",
                    "period": PERIOD,
                    "currency": master_currency,
                    "scale": master_scale,
                    "count": master_count,
                    "checksum": ("m" + token).ljust(64, "b")[:64],
                },
            ).scalar_one()
            for index in range(master_count):
                connection.execute(
                    text(
                        """
                        INSERT INTO tax_master_version (
                            company_id, source_batch_id, valid_from, valid_to, version,
                            status, tax_rate, loss_carryforward,
                            average_tax_burden_rate_3y, deferred_tax_rate,
                            currency, amount_scale,
                            source_file_name, source_checksum, source_row_number,
                            uploaded_by, data, published_at, approved_by
                        )
                        VALUES (
                            :company_id, :batch_id, :valid_from, :valid_to, :version,
                            'PUBLISHED', 0.25, 100.00, 0.08, :deferred_tax_rate,
                            :currency, :scale,
                            'tax-master.xlsx', :checksum, :row_number, 'maker',
                            '{}'::jsonb, now(), 'reviewer'
                        )
                        """
                    ),
                    {
                        "company_id": company_id,
                        "batch_id": master_batch_id,
                        "valid_from": master_valid_from,
                        "valid_to": master_valid_to,
                        "version": f"v{index + 1}",
                        "currency": master_currency,
                        "scale": master_scale,
                        "deferred_tax_rate": deferred_tax_rate,
                        "checksum": (f"{index}{token}").ljust(64, "c")[:64],
                        "row_number": index + 2,
                    },
                )
    return company_code, batch_id


def _validate(
    service: SnapshotService,
    company_code: str,
    batch_id: UUID,
    *,
    accepted_partial: bool = False,
):
    return service.validate(
        company_code=company_code,
        period=PERIOD,
        source_batch_ids=(batch_id,),
        accepted_partial_batch_ids=((batch_id,) if accepted_partial else ()),
    )


@pytest.mark.parametrize("missing_metric", REQUIRED_QUARTERLY_METRICS)
def test_each_missing_required_metric_is_data_quality_not_zero(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    missing_metric: str,
) -> None:
    service, engine, _ = service_resources
    metrics = tuple(metric for metric in REQUIRED_QUARTERLY_METRICS if metric != missing_metric)
    company_code, batch_id = _seed_quality_case(engine, metrics=metrics)

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert result.snapshot is None
    assert any(
        issue.category == "DATA_QUALITY"
        and issue.error_code == "MISSING_REQUIRED_METRIC"
        and issue.field == missing_metric
        for issue in result.issues
    )
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM accounting_snapshot AS snapshot
                JOIN company ON company.id = snapshot.company_id
                WHERE company.company_code = :company_code
                """
            ),
            {"company_code": company_code},
        ).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM detection_record")).scalar_one() == 0


def test_missing_deferred_tax_rate_blocks_quarterly_snapshot_publication(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        deferred_tax_rate=None,
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert result.snapshot is None
    assert any(
        issue.error_code == "DEFERRED_TAX_RATE_MISSING"
        and issue.source == "tax_master"
        and issue.field == "deferred_tax_rate"
        for issue in result.issues
    )


def test_duplicate_metric_blocks_with_stable_code(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    metrics = REQUIRED_QUARTERLY_METRICS + (REQUIRED_QUARTERLY_METRICS[0],)
    company_code, batch_id = _seed_quality_case(engine, metrics=metrics)

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "DUPLICATE_SOURCE_ROW" in {issue.error_code for issue in result.issues}


@pytest.mark.parametrize("status", ["RECEIVED", "VALIDATING", "FAILED"])
def test_nonfinal_source_status_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    status: str,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine, batch_status=status)

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "SOURCE_NOT_READY" in {issue.error_code for issue in result.issues}


def test_succeeded_batch_with_rejections_and_errors_is_internally_inconsistent(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="SUCCEEDED",
        rejected_count=1,
        errors=({"company_code": "OTHER"},),
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "SOURCE_NOT_READY" in {issue.error_code for issue in result.issues}


@pytest.mark.parametrize(
    "details",
    [
        {"company_code": "OTHER-COMPANY"},
        {"company_code": "TARGET", "metric_code": "irrelevant_metric"},
    ],
    ids=["other-company", "same-company-non-required-metric"],
)
def test_explicit_partial_is_accepted_only_with_complete_unrelated_evidence(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    details: dict[str, object],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="PARTIAL",
        rejected_count=1,
        errors=(details,),
    )
    if details.get("company_code") == "TARGET":
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ingest_error
                    SET details = jsonb_set(
                        details, '{company_code}', to_jsonb(CAST(:company_code AS text))
                    )
                    WHERE batch_id = :batch_id
                    """
                ),
                {"company_code": company_code, "batch_id": batch_id},
            )

    result = _validate(service, company_code, batch_id, accepted_partial=True)

    assert result.valid is True, result.issues
    assert result.snapshot is not None
    decision = result.snapshot.lineage["sources"][0]["partial_decision"]
    assert decision["accepted"] is True
    assert decision["safe"] is True
    assert decision["evidence"]


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"metric_code": "irrelevant_metric"},
        {"company_code": "TARGET", "metric_code": "cumulative_profit"},
    ],
    ids=["unknown", "missing-company", "same-company-required"],
)
def test_partial_unknown_or_related_evidence_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    details: dict[str, object],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="PARTIAL",
        rejected_count=1,
        errors=(details,),
    )
    if details.get("company_code") == "TARGET":
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ingest_error
                    SET details = jsonb_set(
                        details, '{company_code}', to_jsonb(CAST(:company_code AS text))
                    )
                    WHERE batch_id = :batch_id
                    """
                ),
                {"company_code": company_code, "batch_id": batch_id},
            )

    result = _validate(service, company_code, batch_id, accepted_partial=True)

    assert result.valid is False
    assert "SOURCE_NOT_READY" in {issue.error_code for issue in result.issues}


def test_partial_rejected_count_must_equal_persisted_error_evidence_count(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="PARTIAL",
        rejected_count=2,
        errors=({"company_code": "OTHER"},),
    )

    result = _validate(service, company_code, batch_id, accepted_partial=True)

    assert result.valid is False
    assert "CONTROL_TOTAL_MISMATCH" in {issue.error_code for issue in result.issues}


def test_partial_noncanonical_error_details_are_unproven_data_quality(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="PARTIAL",
        rejected_count=1,
        errors=({"company_code": "OTHER", "confidence": 0.75},),
    )

    result = _validate(service, company_code, batch_id, accepted_partial=True)

    assert result.valid is False
    assert result.snapshot is None
    assert any(
        issue.error_code == "NON_CANONICAL_JSON"
        and issue.field == "ingest_error.details"
        for issue in result.issues
    )


@pytest.mark.parametrize("json_value", ["null", "[]", '"text"', "7"])
def test_partial_nonobject_error_details_are_unproven_data_quality(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    json_value: str,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        batch_status="PARTIAL",
        rejected_count=1,
        errors=({"company_code": "OTHER"},),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ingest_error
                SET details = CAST(:json_value AS jsonb)
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": batch_id, "json_value": json_value},
        )

    result = _validate(service, company_code, batch_id, accepted_partial=True)

    assert result.valid is False
    assert result.snapshot is None
    assert any(
        issue.error_code == "NON_CANONICAL_JSON"
        and issue.field == "ingest_error.details"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("payload_override", "expected_field"),
    [
        ({"currency": "USD"}, "payload.currency"),
        ({"amount_scale": 3}, "payload.amount_scale"),
        ({"period": "2026-03-31"}, "payload.period"),
        ({"amount": "999.99"}, "payload.amount"),
    ],
)
def test_source_payload_drift_blocks_snapshot(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    payload_override: dict[str, object],
    expected_field: str,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        payload_overrides=payload_override,
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert any(issue.field == expected_field for issue in result.issues)


def test_exact_batch_control_total_mismatch_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        control_total_delta=Decimal("0.000000000001"),
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "CONTROL_TOTAL_MISMATCH" in {issue.error_code for issue in result.issues}


@pytest.mark.parametrize(
    ("master_count", "expected_code"),
    [(0, "TAX_MASTER_MISSING"), (2, "TAX_MASTER_DUPLICATE")],
)
def test_tax_master_cardinality_is_exactly_one(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    master_count: int,
    expected_code: str,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine, master_count=master_count)

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert expected_code in {issue.error_code for issue in result.issues}


def test_master_effective_boundaries_and_zero_negative_metrics_are_valid(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    values = (
        Decimal("0"),
        Decimal("-1.00"),
        Decimal("2.00"),
        Decimal("-3.00"),
        Decimal("4.00"),
        Decimal("0.00"),
        Decimal("-5.00"),
        Decimal("6.00"),
        Decimal("-7.00"),
    )
    company_code, batch_id = _seed_quality_case(
        engine,
        values=values,
        master_valid_from=PERIOD,
        master_valid_to=PERIOD,
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is True, result.issues
    assert result.snapshot is not None
    amounts = [metric["amount"] for metric in result.snapshot.lineage["metrics"]]
    assert amounts == [f"{value:.12f}" for value in values]
    assert result.snapshot.record_count == len(REQUIRED_QUARTERLY_METRICS)
    assert result.snapshot.control_total == sum(values, Decimal("0"))


def test_initially_inactive_company_is_unmapped_for_snapshot_quality(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        company_lifecycle="INACTIVE",
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "COMPANY_UNMAPPED" in {issue.error_code for issue in result.issues}


@pytest.mark.parametrize(
    ("mismatch", "expected_field"),
    [
        ("period", "period"),
        ("currency", "currency_amount_scale"),
        ("scale", "currency_amount_scale"),
        ("dataset", "dataset_code"),
    ],
)
def test_batch_period_currency_scale_and_dataset_mismatch_block(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    mismatch: str,
    expected_field: str,
) -> None:
    service, engine, _ = service_resources
    kwargs: dict[str, object] = {}
    if mismatch == "period":
        kwargs["batch_period"] = date(2026, 3, 31)
    elif mismatch == "currency":
        kwargs["batch_currency"] = "USD"
    elif mismatch == "scale":
        kwargs["batch_scale"] = 3
    company_code, batch_id = _seed_quality_case(engine, **kwargs)  # type: ignore[arg-type]
    if mismatch == "dataset":
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE ingest_batch SET dataset_code = 'other' WHERE id = :batch_id"),
                {"batch_id": batch_id},
            )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert any(issue.field == expected_field for issue in result.issues)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset_code", "other"),
        ("period", date(2026, 3, 31)),
        ("currency", "USD"),
        ("amount_scale", 3),
    ],
)
def test_typed_source_record_metadata_drift_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    field: str,
    value: object,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        row_overrides={field: value},
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "SOURCE_METADATA_MISMATCH" in {issue.error_code for issue in result.issues}


def test_source_record_company_drift_removes_target_contribution_and_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine)
    with engine.begin() as connection:
        other_company = connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:code, 'Other company')
                RETURNING id
                """
            ),
            {"code": f"OTHER-{uuid4().hex}"},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE source_record SET company_id = :company_id WHERE batch_id = :batch_id"
            ),
            {"company_id": other_company, "batch_id": batch_id},
        )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "SOURCE_NOT_READY" in {issue.error_code for issue in result.issues}
    assert "MISSING_REQUIRED_METRIC" in {issue.error_code for issue in result.issues}


def test_accepted_count_must_equal_all_persisted_source_records(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ingest_batch
                SET record_count = 7, accepted_count = 7
                WHERE id = :batch_id
                """
            ),
            {"batch_id": batch_id},
        )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert any(
        issue.error_code == "CONTROL_TOTAL_MISMATCH"
        and issue.field == "accepted_count"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("currency", "scale"),
    [("USD", 2), ("CNY", 3)],
)
def test_master_currency_or_scale_mismatch_blocks(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    currency: str,
    scale: int,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(
        engine,
        master_currency=currency,
        master_scale=scale,
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is False
    assert "TAX_MASTER_METADATA_MISMATCH" in {
        issue.error_code for issue in result.issues
    }


def test_unknown_selected_batch_is_source_not_ready_and_writes_nothing(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, _ = _seed_quality_case(engine)

    result = service.validate(
        company_code=company_code,
        period=PERIOD,
        source_batch_ids=(uuid4(),),
    )

    assert result.valid is False
    assert "SOURCE_NOT_READY" in {issue.error_code for issue in result.issues}
    assert result.snapshot is None


def test_metric_value_change_changes_source_hash_and_full_checksum_even_if_batch_hash_is_stale(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine)
    first = _validate(service, company_code, batch_id)
    assert first.valid is True and first.snapshot is not None
    with engine.begin() as connection:
        record_id = connection.execute(
            text(
                """
                SELECT id FROM source_record
                WHERE batch_id = :batch_id
                  AND payload ->> 'metric_code' = 'cumulative_profit'
                """
            ),
            {"batch_id": batch_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                UPDATE source_record
                SET amount = amount + 1,
                    payload = jsonb_set(payload, '{amount}', to_jsonb('2.000000000000'::text))
                WHERE id = :record_id
                """
            ),
            {"record_id": record_id},
        )
        connection.execute(
            text("UPDATE ingest_batch SET control_total = control_total + 1 WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )

    second = _validate(service, company_code, batch_id)

    assert second.valid is True, second.issues
    assert second.snapshot is not None
    assert second.snapshot.id != first.snapshot.id
    assert second.snapshot.source_version_set_hash != first.snapshot.source_version_set_hash
    assert second.snapshot.checksum != first.snapshot.checksum


def test_snapshot_freezes_source_extraction_and_tax_master_import_identity(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id = _seed_quality_case(engine)
    extraction_time = datetime(2026, 7, 1, 8, 15, 30, tzinfo=timezone.utc)
    imported_at = datetime(2026, 7, 1, 9, 45, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ingest_batch
                SET extraction_time = :extraction_time,
                    payload_ref = 'sap-quarterly-2026-q2.csv'
                WHERE id = :batch_id
                """
            ),
            {"extraction_time": extraction_time, "batch_id": batch_id},
        )
        connection.execute(
            text(
                """
                UPDATE tax_master_version
                SET source_file_name = 'group-tax-master-2026.xlsx',
                    created_at = :imported_at
                WHERE company_id = (
                    SELECT company_id FROM source_record
                    WHERE batch_id = :batch_id LIMIT 1
                )
                """
            ),
            {"imported_at": imported_at, "batch_id": batch_id},
        )

    captured: dict[str, object] = {}
    original_hash = snapshots_application.source_version_set_hash

    def capture_identities(
        batches: Sequence[dict[str, object]],
        master: dict[str, object],
    ) -> str:
        captured["batches"] = batches
        captured["master"] = master
        return original_hash(batches, master)

    monkeypatch.setattr(
        snapshots_application,
        "source_version_set_hash",
        capture_identities,
    )

    result = _validate(service, company_code, batch_id)

    assert result.valid is True, result.issues
    assert result.snapshot is not None
    assert result.snapshot.lineage["schema_version"] == "quarterly-accounting-snapshot-v2"
    source_batch = result.snapshot.lineage["sources"][0]["batch"]
    assert source_batch["extraction_time"] == "2026-07-01T08:15:30Z"
    assert source_batch["payload_ref"] == "sap-quarterly-2026-q2.csv"
    master_lineage = result.snapshot.lineage["tax_master"]
    assert master_lineage["deferred_tax_rate"] == "0.200000000000"
    assert master_lineage["source_file_name"] == "group-tax-master-2026.xlsx"
    assert master_lineage["imported_at"] == "2026-07-01T09:45:00Z"
    captured_batch = captured["batches"]
    assert isinstance(captured_batch, list)
    assert captured_batch[0]["extraction_time"] == "2026-07-01T08:15:30Z"
    assert captured_batch[0]["payload_ref"] == "sap-quarterly-2026-q2.csv"
    captured_master = captured["master"]
    assert isinstance(captured_master, dict)
    assert captured_master["source_file_name"] == "group-tax-master-2026.xlsx"
    assert captured_master["imported_at"] == "2026-07-01T09:45:00Z"


def test_selected_batch_input_order_reuses_identical_draft_hash_and_sources(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, first_batch = _seed_quality_case(engine)
    with engine.begin() as connection:
        moved = connection.execute(
            text(
                """
                SELECT id, amount FROM source_record
                WHERE batch_id = :batch_id
                ORDER BY source_record_key
                LIMIT 4
                """
            ),
            {"batch_id": first_batch},
        ).all()
        moved_ids = [row.id for row in moved]
        moved_total = sum((row.amount for row in moved), Decimal("0"))
        second_batch = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'HESI', :key, 'quarterly_metric', 'SUCCEEDED', now(),
                    :period, 'FULL', 'quarterly-v1', 'CNY', 2, 4, 4, 0,
                    :control_total, repeat('2', 64)
                ) RETURNING id
                """
            ),
            {
                "key": f"split-{uuid4().hex}",
                "period": PERIOD,
                "control_total": moved_total,
            },
        ).scalar_one()
        connection.execute(
            text("UPDATE source_record SET batch_id = :new_batch WHERE id = ANY(:record_ids)"),
            {"new_batch": second_batch, "record_ids": moved_ids},
        )
        connection.execute(
            text(
                """
                UPDATE ingest_batch
                SET record_count = :remaining_count,
                    accepted_count = :remaining_count,
                    control_total = control_total - :moved_total
                WHERE id = :batch_id
                """
            ),
            {
                "batch_id": first_batch,
                "moved_total": moved_total,
                "remaining_count": len(REQUIRED_QUARTERLY_METRICS) - len(moved_ids),
            },
        )

    first = service.validate(
        company_code=company_code,
        period=PERIOD,
        source_batch_ids=(first_batch, second_batch),
    )
    reordered = service.validate(
        company_code=company_code,
        period=PERIOD,
        source_batch_ids=(second_batch, first_batch),
    )

    assert first.valid is True and first.snapshot is not None
    assert reordered.valid is True and reordered.snapshot is not None
    assert reordered.reused is True
    assert reordered.snapshot.id == first.snapshot.id
    assert reordered.snapshot.source_version_set_hash == first.snapshot.source_version_set_hash
    assert reordered.snapshot.checksum == first.snapshot.checksum
    with engine.connect() as connection:
        sources = connection.execute(
            text(
                """
                SELECT record_count FROM snapshot_source
                WHERE snapshot_id = :snapshot_id
                ORDER BY ingest_batch_id
                """
            ),
            {"snapshot_id": first.snapshot.id},
        ).scalars().all()
    assert sorted(sources) == [4, len(REQUIRED_QUARTERLY_METRICS) - 4]


def _draft_snapshot(
    service: SnapshotService,
    engine: Engine,
) -> tuple[str, UUID, UUID]:
    company_code, batch_id = _seed_quality_case(engine)
    result = _validate(service, company_code, batch_id)
    assert result.valid is True, result.issues
    assert result.snapshot is not None
    assert result.snapshot.status == SnapshotStatus.DRAFT
    return company_code, batch_id, result.snapshot.id


def test_publish_locks_snapshot_sources_and_batches_before_company_and_master(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    _, _, snapshot_id = _draft_snapshot(service, engine)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "for update" in normalized or "for share" in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        service.publish(snapshot_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    def first(table: str) -> int:
        return next(index for index, statement in enumerate(statements) if table in statement)

    assert first("from accounting_snapshot") < first("from snapshot_source")
    assert first("from snapshot_source") < first("from ingest_batch")
    assert first("from ingest_batch") < first("from source_record")
    assert first("from source_record") < first("from ingest_error")
    assert first("from ingest_error") < first("from company")
    assert first("from company") < first("from tax_master_version")


def test_replayed_validation_locks_existing_snapshot_sources_before_quality_data(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, batch_id, snapshot_id = _draft_snapshot(service, engine)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "for update" in normalized or "for share" in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        replayed = _validate(service, company_code, batch_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert replayed.valid is True
    assert replayed.reused is True
    assert replayed.snapshot is not None
    assert replayed.snapshot.id == snapshot_id

    def first(table: str) -> int:
        return next(index for index, statement in enumerate(statements) if table in statement)

    assert first("from accounting_snapshot") < first("from snapshot_source")
    assert first("from snapshot_source") < first("from ingest_batch")
    assert first("from ingest_batch") < first("from source_record")
    assert first("from source_record") < first("from ingest_error")
    assert first("from ingest_error") < first("from company")
    assert first("from company") < first("from tax_master_version")


def test_publish_reruns_gate_and_writes_database_utc_once(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    company_code, _, snapshot_id = _draft_snapshot(service, engine)

    published = service.publish(snapshot_id)

    assert published.company_code == company_code
    assert published.status == SnapshotStatus.PUBLISHED
    assert published.published_at is not None
    assert published.published_at.utcoffset() is not None
    with engine.connect() as connection:
        persisted = connection.execute(
            text(
                """
                SELECT status, published_at, count(source.id)
                FROM accounting_snapshot AS snapshot
                JOIN snapshot_source AS source ON source.snapshot_id = snapshot.id
                WHERE snapshot.id = :snapshot_id
                GROUP BY snapshot.status, snapshot.published_at
                """
            ),
            {"snapshot_id": snapshot_id},
        ).one()
    assert persisted.status == "PUBLISHED"
    assert persisted.published_at == published.published_at
    assert persisted.count == 1

    with pytest.raises(SnapshotConflictError) as conflict:
        service.publish(snapshot_id)
    assert conflict.value.error_code == "SNAPSHOT_STATE_CONFLICT"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT published_at FROM accounting_snapshot WHERE id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one() == published.published_at


def test_publish_detects_source_drift_after_draft_and_rolls_back_to_draft(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, _ = service_resources
    _, batch_id, snapshot_id = _draft_snapshot(service, engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ingest_batch
                SET control_total = control_total + 0.01,
                    checksum = repeat('d', 64)
                WHERE id = :batch_id
                """
            ),
            {"batch_id": batch_id},
        )

    with pytest.raises(SnapshotQualityError) as quality:
        service.publish(snapshot_id)

    assert {
        issue.error_code for issue in quality.value.issues
    } & {"CONTROL_TOTAL_MISMATCH", "FROZEN_SNAPSHOT_MISMATCH"}
    with engine.connect() as connection:
        status_row = connection.execute(
            text(
                """
                SELECT status, published_at
                FROM accounting_snapshot
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).one()
    assert status_row.status == "DRAFT"
    assert status_row.published_at is None


def test_publish_lock_rereads_company_after_concurrent_deactivation(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, factory = service_resources
    company_code, _, snapshot_id = _draft_snapshot(service, engine)
    rendezvous = Barrier(2, timeout=5)

    class CoordinatedIngestRepository(IngestRepository):
        def get_company(self, company_id: UUID):  # type: ignore[no-untyped-def]
            company = super().get_company(company_id)
            rendezvous.wait()
            rendezvous.wait()
            return company

    class CoordinatedUnitOfWork(UnitOfWork):
        def __enter__(self):  # type: ignore[no-untyped-def]
            entered = super().__enter__()
            self.ingest = CoordinatedIngestRepository(self.session)
            return entered

    coordinated = SnapshotService(partial(CoordinatedUnitOfWork, factory))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(coordinated.publish, snapshot_id)
        rendezvous.wait()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE company
                    SET lifecycle = 'INACTIVE', deactivated_at = now(),
                        lifecycle_reason = 'concurrent test'
                    WHERE company_code = :company_code
                    """
                ),
                {"company_code": company_code},
            )
        rendezvous.wait()
        with pytest.raises(SnapshotQualityError) as quality:
            future.result(timeout=5)

    assert "COMPANY_UNMAPPED" in {issue.error_code for issue in quality.value.issues}
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM accounting_snapshot WHERE id = :id"),
            {"id": snapshot_id},
        ).scalar_one() == "DRAFT"


@pytest.mark.parametrize(
    ("target", "mutation", "expected_code"),
    [
        (
            "batch",
            "UPDATE ingest_batch SET status = 'FAILED' WHERE id = :target_id",
            "SOURCE_NOT_READY",
        ),
        (
            "master",
            "UPDATE tax_master_version SET status = 'RETIRED' WHERE id = :target_id",
            "TAX_MASTER_MISSING",
        ),
        (
            "master",
            "UPDATE tax_master_version SET tax_rate = 0.20 WHERE id = :target_id",
            "FROZEN_SNAPSHOT_MISMATCH",
        ),
    ],
    ids=["batch-status", "master-status", "master-value"],
)
def test_publish_lock_rechecks_batch_and_master_drift(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    target: str,
    mutation: str,
    expected_code: str,
) -> None:
    service, engine, _ = service_resources
    _, batch_id, snapshot_id = _draft_snapshot(service, engine)
    with engine.connect() as connection:
        master_id = connection.execute(
            text(
                "SELECT tax_master_version_id FROM accounting_snapshot WHERE id = :id"
            ),
            {"id": snapshot_id},
        ).scalar_one()
    target_id = batch_id if target == "batch" else master_id
    with engine.begin() as connection:
        connection.execute(text(mutation), {"target_id": target_id})

    with pytest.raises(SnapshotQualityError) as quality:
        service.publish(snapshot_id)

    assert expected_code in {issue.error_code for issue in quality.value.issues}
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM accounting_snapshot WHERE id = :id"),
            {"id": snapshot_id},
        ).scalar_one() == "DRAFT"


@pytest.mark.parametrize("stage", ["snapshot_draft_created", "snapshot_source_created"])
def test_validate_failure_injection_rolls_back_draft_and_sources(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    stage: str,
) -> None:
    _, engine, factory = service_resources
    company_code, batch_id = _seed_quality_case(engine)

    def fail_at(actual: str) -> None:
        if actual == stage:
            raise RuntimeError(f"injected at {stage}")

    failing = SnapshotService(
        partial(UnitOfWork, factory),
        failure_injector=fail_at,
    )
    with pytest.raises(RuntimeError, match=stage):
        _validate(failing, company_code, batch_id)

    with engine.connect() as connection:
        counts = connection.execute(
            text(
                """
                SELECT
                    count(DISTINCT snapshot.id) AS snapshots,
                    count(source.id) AS sources
                FROM company
                LEFT JOIN accounting_snapshot AS snapshot
                  ON snapshot.company_id = company.id
                LEFT JOIN snapshot_source AS source
                  ON source.snapshot_id = snapshot.id
                WHERE company.company_code = :company_code
                """
            ),
            {"company_code": company_code},
        ).one()
    assert counts.snapshots == 0
    assert counts.sources == 0


def test_publish_failure_after_validated_flush_rolls_back_to_draft(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, factory = service_resources
    _, _, snapshot_id = _draft_snapshot(service, engine)

    def fail_at(stage: str) -> None:
        if stage == "snapshot_validated":
            raise RuntimeError("injected after validated flush")

    failing = SnapshotService(
        partial(UnitOfWork, factory),
        failure_injector=fail_at,
    )
    with pytest.raises(RuntimeError, match="validated"):
        failing.publish(snapshot_id)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, published_at FROM accounting_snapshot WHERE id = :id"
            ),
            {"id": snapshot_id},
        ).one()
    assert row.status == "DRAFT"
    assert row.published_at is None


@pytest.mark.parametrize(
    ("target", "operation"),
    [
        ("snapshot", "UPDATE"),
        ("snapshot", "DELETE"),
        ("source", "UPDATE"),
        ("source", "DELETE"),
        ("source", "INSERT"),
    ],
)
def test_published_snapshot_and_frozen_sources_are_database_immutable(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
    target: str,
    operation: str,
) -> None:
    service, engine, _ = service_resources
    _, batch_id, snapshot_id = _draft_snapshot(service, engine)
    service.publish(snapshot_id)
    with engine.connect() as connection:
        transaction = connection.begin()
        source_id = connection.execute(
            text("SELECT id FROM snapshot_source WHERE snapshot_id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one()
        if target == "snapshot":
            statement = (
                "UPDATE accounting_snapshot SET checksum = repeat('e', 64) WHERE id = :id"
                if operation == "UPDATE"
                else "DELETE FROM accounting_snapshot WHERE id = :id"
            )
            params = {"id": snapshot_id}
        elif operation == "INSERT":
            statement = """
                INSERT INTO snapshot_source (
                    snapshot_id, ingest_batch_id, source, source_version,
                    record_count, control_total, currency, amount_scale, lineage
                )
                VALUES (
                    :snapshot_id, :batch_id, 'SAP', 'late', 0, 0,
                    'CNY', 2, '{}'::jsonb
                )
            """
            params = {"snapshot_id": snapshot_id, "batch_id": batch_id}
        else:
            statement = (
                "UPDATE snapshot_source SET source_version = 'changed' WHERE id = :id"
                if operation == "UPDATE"
                else "DELETE FROM snapshot_source WHERE id = :id"
            )
            params = {"id": source_id}
        with pytest.raises(DBAPIError, match="immutable_snapshot"):
            connection.execute(text(statement), params)
        transaction.rollback()


def test_concurrent_publish_has_one_success_and_one_stable_conflict(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, factory = service_resources
    _, _, snapshot_id = _draft_snapshot(service, engine)

    def publish_once() -> SnapshotView | SnapshotConflictError:
        worker = SnapshotService(partial(UnitOfWork, factory))
        try:
            return worker.publish(snapshot_id)
        except SnapshotConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: publish_once(), range(2)))

    successes = [outcome for outcome in outcomes if isinstance(outcome, SnapshotView)]
    conflicts = [
        outcome for outcome in outcomes if isinstance(outcome, SnapshotConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].error_code == "SNAPSHOT_STATE_CONFLICT"


def test_publish_commit_is_the_last_database_action(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    _, engine, factory = service_resources
    states: list[dict[str, bool]] = []

    class TrackingUnitOfWork(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(factory)
            self.state = {"committed": False}
            states.append(self.state)

        def commit(self) -> None:
            super().commit()
            self.state["committed"] = True

    def reject_sql_after_commit(
        _connection: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if states and states[-1]["committed"]:
            raise AssertionError("database action occurred after application commit")

    event.listen(engine, "before_cursor_execute", reject_sql_after_commit)
    try:
        tracked = SnapshotService(TrackingUnitOfWork)
        _, _, snapshot_id = _draft_snapshot(tracked, engine)
        published = tracked.publish(snapshot_id)
    finally:
        event.remove(engine, "before_cursor_execute", reject_sql_after_commit)

    assert published.status == SnapshotStatus.PUBLISHED
    assert states[-1]["committed"] is True


@pytest.fixture(scope="module")
def set_resources(
    isolated_database_url: str,
) -> Iterator[
    tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ]
]:
    engine, factory = create_session_factory(isolated_database_url)
    service = SnapshotService(partial(UnitOfWork, factory))
    members: list[ExpectedSnapshotMember] = []
    batch_by_snapshot: dict[UUID, UUID] = {}
    try:
        for _ in range(101):
            company_code, batch_id = _seed_quality_case(engine)
            validated = _validate(service, company_code, batch_id)
            assert validated.valid is True, validated.issues
            assert validated.snapshot is not None
            published = service.publish(validated.snapshot.id)
            members.append(
                ExpectedSnapshotMember(
                    company_id=published.company_id,
                    snapshot_id=published.id,
                )
            )
            batch_by_snapshot[published.id] = batch_id
        draft_code, draft_batch = _seed_quality_case(engine)
        draft = _validate(service, draft_code, draft_batch)
        assert draft.valid is True and draft.snapshot is not None
        draft_member = ExpectedSnapshotMember(
            company_id=draft.snapshot.company_id,
            snapshot_id=draft.snapshot.id,
        )
        yield (
            service,
            engine,
            factory,
            tuple(members),
            draft_member,
            batch_by_snapshot,
        )
    finally:
        engine.dispose()


def _set_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _assert_set_absent(engine: Engine, set_key: str) -> None:
    with engine.connect() as connection:
        counts = connection.execute(
            text(
                """
                SELECT
                    count(DISTINCT snapshot_set.id) AS sets,
                    count(member.id) AS members
                FROM snapshot_set
                LEFT JOIN snapshot_set_member AS member
                  ON member.snapshot_set_id = snapshot_set.id
                WHERE snapshot_set.set_key = :set_key
                """
            ),
            {"set_key": set_key},
        ).one()
    assert counts.sets == 0
    assert counts.members == 0


def test_snapshot_set_rejects_99_members_without_writes(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    service, engine, _, members, _, _ = set_resources
    set_key = _set_key("set-99")

    with pytest.raises(SnapshotRequestError) as invalid:
        service.publish_set(
            set_key=set_key,
            period=PERIOD,
            expected_members=members[:99],
        )

    assert invalid.value.error_code == "SNAPSHOT_SET_TOO_SMALL"
    _assert_set_absent(engine, set_key)


@pytest.mark.parametrize("member_count", [100, 101])
def test_snapshot_set_publishes_complete_100_or_101_member_identity_atomically(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
    member_count: int,
) -> None:
    service, engine, _, members, _, _ = set_resources
    set_key = _set_key(f"set-{member_count}")

    published = service.publish_set(
        set_key=set_key,
        period=PERIOD,
        expected_members=members[:member_count],
    )

    assert published.status == SnapshotSetStatus.PUBLISHED
    assert published.expected_member_count == member_count
    assert published.members == tuple(sorted(members[:member_count], key=lambda item: item.company_id))
    assert published.published_at.utcoffset() is not None
    assert not hasattr(published, "data_ready_at")
    with engine.connect() as connection:
        persisted = connection.execute(
            text(
                """
                SELECT snapshot_set.status, snapshot_set.published_at,
                       count(member.id) AS member_count
                FROM snapshot_set
                JOIN snapshot_set_member AS member
                  ON member.snapshot_set_id = snapshot_set.id
                WHERE snapshot_set.id = :set_id
                GROUP BY snapshot_set.status, snapshot_set.published_at
                """
            ),
            {"set_id": published.id},
        ).one()
    assert persisted.status == "PUBLISHED"
    assert persisted.published_at == published.published_at
    assert persisted.member_count == member_count


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "identity-substitution",
        "mixed-period",
        "missing-snapshot",
        "duplicate-company",
        "duplicate-snapshot",
        "nonpublished",
    ],
)
def test_snapshot_set_rejects_invalid_complete_member_lists_without_writes(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
    invalid_kind: str,
) -> None:
    service, engine, _, members, draft_member, _ = set_resources
    set_key = _set_key(f"invalid-{invalid_kind}")
    candidates = list(members[:100])
    requested_period = PERIOD
    if invalid_kind == "identity-substitution":
        first_company = candidates[0].company_id
        second_company = candidates[1].company_id
        candidates[0] = ExpectedSnapshotMember(
            company_id=second_company,
            snapshot_id=candidates[0].snapshot_id,
        )
        candidates[1] = ExpectedSnapshotMember(
            company_id=first_company,
            snapshot_id=candidates[1].snapshot_id,
        )
    elif invalid_kind == "mixed-period":
        requested_period = date(2026, 3, 31)
    elif invalid_kind == "missing-snapshot":
        candidates[0] = ExpectedSnapshotMember(
            company_id=candidates[0].company_id,
            snapshot_id=uuid4(),
        )
    elif invalid_kind == "duplicate-company":
        candidates[1] = ExpectedSnapshotMember(
            company_id=candidates[0].company_id,
            snapshot_id=candidates[1].snapshot_id,
        )
    elif invalid_kind == "duplicate-snapshot":
        candidates[1] = ExpectedSnapshotMember(
            company_id=candidates[1].company_id,
            snapshot_id=candidates[0].snapshot_id,
        )
    elif invalid_kind == "nonpublished":
        candidates[0] = draft_member

    with pytest.raises(SnapshotRequestError):
        service.publish_set(
            set_key=set_key,
            period=requested_period,
            expected_members=candidates,
        )

    _assert_set_absent(engine, set_key)


@pytest.mark.parametrize(
    "stage",
    [
        "snapshot_set_draft_created",
        "snapshot_set_member_created",
        "snapshot_set_validated",
        "snapshot_set_published",
    ],
)
def test_snapshot_set_failure_injection_rolls_back_set_members_and_timestamp(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
    stage: str,
) -> None:
    _, engine, factory, members, _, _ = set_resources
    set_key = _set_key(f"failure-{stage}")

    def fail_at(actual: str) -> None:
        if actual == stage:
            raise RuntimeError(f"injected at {stage}")

    failing = SnapshotService(
        partial(UnitOfWork, factory),
        failure_injector=fail_at,
    )
    with pytest.raises(RuntimeError, match=stage):
        failing.publish_set(
            set_key=set_key,
            period=PERIOD,
            expected_members=members[:100],
        )

    _assert_set_absent(engine, set_key)


def test_snapshot_set_supersedes_only_published_same_period_and_preserves_old_set(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    service, engine, _, members, _, _ = set_resources
    old_set = service.publish_set(
        set_key=_set_key("superseded"),
        period=PERIOD,
        expected_members=members[:100],
    )

    replacement = service.publish_set(
        set_key=_set_key("replacement"),
        period=PERIOD,
        expected_members=members[:100],
        supersedes_snapshot_set_id=old_set.id,
    )

    assert replacement.supersedes_snapshot_set_id == old_set.id
    with engine.connect() as connection:
        old_row = connection.execute(
            text(
                """
                SELECT snapshot_set.status, snapshot_set.published_at,
                       count(member.id) AS member_count
                FROM snapshot_set
                JOIN snapshot_set_member AS member
                  ON member.snapshot_set_id = snapshot_set.id
                WHERE snapshot_set.id = :set_id
                GROUP BY snapshot_set.status, snapshot_set.published_at
                """
            ),
            {"set_id": old_set.id},
        ).one()
    assert old_row.status == "PUBLISHED"
    assert old_row.published_at == old_set.published_at
    assert old_row.member_count == 100


def test_snapshot_set_member_quality_drift_rolls_back_entire_set(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    service, engine, _, members, _, batch_by_snapshot = set_resources
    set_key = _set_key("quality-drift")
    drift_batch = batch_by_snapshot[members[0].snapshot_id]
    with engine.begin() as connection:
        original_checksum = connection.execute(
            text("SELECT checksum FROM ingest_batch WHERE id = :batch_id"),
            {"batch_id": drift_batch},
        ).scalar_one()
        connection.execute(
            text("UPDATE ingest_batch SET checksum = repeat('f', 64) WHERE id = :batch_id"),
            {"batch_id": drift_batch},
        )
    try:
        with pytest.raises(SnapshotQualityError):
            service.publish_set(
                set_key=set_key,
                period=PERIOD,
                expected_members=members[:100],
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE ingest_batch SET checksum = :checksum WHERE id = :batch_id"),
                {"checksum": original_checksum, "batch_id": drift_batch},
            )

    _assert_set_absent(engine, set_key)


@pytest.mark.parametrize(
    ("target", "operation"),
    [
        ("set", "UPDATE"),
        ("set", "DELETE"),
        ("member", "UPDATE"),
        ("member", "DELETE"),
        ("member", "INSERT"),
    ],
)
def test_published_snapshot_set_and_members_are_database_immutable(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
    target: str,
    operation: str,
) -> None:
    service, engine, _, members, _, _ = set_resources
    published = service.publish_set(
        set_key=_set_key("immutable-set"),
        period=PERIOD,
        expected_members=members[:100],
    )
    with engine.connect() as connection:
        transaction = connection.begin()
        member_id = connection.execute(
            text(
                "SELECT id FROM snapshot_set_member WHERE snapshot_set_id = :set_id LIMIT 1"
            ),
            {"set_id": published.id},
        ).scalar_one()
        if target == "set":
            statement = (
                "UPDATE snapshot_set SET set_key = 'changed' WHERE id = :id"
                if operation == "UPDATE"
                else "DELETE FROM snapshot_set WHERE id = :id"
            )
            params = {"id": published.id}
        elif operation == "INSERT":
            statement = """
                INSERT INTO snapshot_set_member (
                    snapshot_set_id, company_id, snapshot_id
                ) VALUES (:set_id, :company_id, :snapshot_id)
            """
            params = {
                "set_id": published.id,
                "company_id": members[100].company_id,
                "snapshot_id": members[100].snapshot_id,
            }
        else:
            statement = (
                "UPDATE snapshot_set_member SET company_id = company_id WHERE id = :id"
                if operation == "UPDATE"
                else "DELETE FROM snapshot_set_member WHERE id = :id"
            )
            params = {"id": member_id}
        with pytest.raises(DBAPIError, match="immutable_snapshot"):
            connection.execute(text(statement), params)
        transaction.rollback()


def test_snapshot_set_locks_all_dependency_classes_in_global_order(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    service, engine, _, members, _, _ = set_resources
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "for update" in normalized or "for share" in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        service.publish_set(
            set_key=_set_key("lock-order"),
            period=PERIOD,
            expected_members=tuple(reversed(members[:100])),
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    def first(table: str) -> int:
        return next(index for index, statement in enumerate(statements) if table in statement)

    assert first("from accounting_snapshot") < first("from snapshot_source")
    assert first("from snapshot_source") < first("from ingest_batch")
    assert first("from ingest_batch") < first("from source_record")
    assert first("from source_record") < first("from ingest_error")
    assert first("from ingest_error") < first("from company")
    assert first("from company") < first("from tax_master_version")


def test_snapshot_set_uses_one_company_probe_and_one_effective_master_query(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    service, engine, _, members, _, _ = set_resources
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        service.publish_set(
            set_key=_set_key("bulk-query-shape"),
            period=PERIOD,
            expected_members=members[:100],
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    company_probes = [
        statement
        for statement in statements
        if " from company " in f" {statement} "
        and "company.id in" in statement
        and "for share" not in statement
    ]
    master_locks = [
        statement
        for statement in statements
        if " from tax_master_version " in f" {statement} "
        and "for update" in statement
        and "tax_master_version.company_id in" in statement
    ]
    assert len(company_probes) == 1, company_probes
    assert len(master_locks) == 1, master_locks


def test_snapshot_set_groups_quality_rows_once_before_member_evaluation(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, members, _, _ = set_resources
    original = getattr(snapshots_application, "_group_rows_by_batch", None)
    assert original is not None, "set publication must expose one-pass batch grouping"
    calls = 0

    def counting_group(rows: object):
        nonlocal calls
        calls += 1
        return original(rows)

    monkeypatch.setattr(snapshots_application, "_group_rows_by_batch", counting_group)

    service.publish_set(
        set_key=_set_key("grouped-quality-data"),
        period=PERIOD,
        expected_members=members[:100],
    )

    assert calls == 2


def test_snapshot_sets_with_opposite_member_input_order_do_not_deadlock(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    _, _, factory, members, _, _ = set_resources

    def publish(key: str, expected: Sequence[ExpectedSnapshotMember]) -> SnapshotSetView:
        return SnapshotService(partial(UnitOfWork, factory)).publish_set(
            set_key=key,
            period=PERIOD,
            expected_members=expected,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(publish, _set_key("order-left"), members[:100])
        right = executor.submit(
            publish,
            _set_key("order-right"),
            tuple(reversed(members[:100])),
        )
        results = (left.result(timeout=30), right.result(timeout=30))

    assert all(result.status == SnapshotSetStatus.PUBLISHED for result in results)


def test_concurrent_same_snapshot_set_key_has_one_success_and_stable_conflict(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    _, _, factory, members, _, _ = set_resources
    set_key = _set_key("same-key")

    def publish() -> SnapshotSetView | SnapshotConflictError:
        try:
            return SnapshotService(partial(UnitOfWork, factory)).publish_set(
                set_key=set_key,
                period=PERIOD,
                expected_members=members[:100],
            )
        except SnapshotConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: publish(), range(2)))

    assert sum(isinstance(item, SnapshotSetView) for item in outcomes) == 1
    conflicts = [item for item in outcomes if isinstance(item, SnapshotConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].error_code == "SNAPSHOT_SET_KEY_CONFLICT"


def test_snapshot_set_commit_is_the_last_database_action(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    _, engine, factory, members, _, _ = set_resources
    states: list[dict[str, bool]] = []

    class TrackingUnitOfWork(UnitOfWork):
        def __init__(self) -> None:
            super().__init__(factory)
            self.state = {"committed": False}
            states.append(self.state)

        def commit(self) -> None:
            super().commit()
            self.state["committed"] = True

    def reject_sql_after_commit(
        _connection: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if states and states[-1]["committed"]:
            raise AssertionError("database action occurred after snapshot-set commit")

    event.listen(engine, "before_cursor_execute", reject_sql_after_commit)
    try:
        published = SnapshotService(TrackingUnitOfWork).publish_set(
            set_key=_set_key("commit-last-set"),
            period=PERIOD,
            expected_members=members[:100],
        )
    finally:
        event.remove(engine, "before_cursor_execute", reject_sql_after_commit)

    assert published.status == SnapshotSetStatus.PUBLISHED
    assert states[-1]["committed"] is True


def _insert_overlapping_draft_master(engine: Engine, company_id: UUID) -> UUID:
    token = uuid4().hex
    with engine.begin() as connection:
        company_name, source_batch_id = connection.execute(
            text(
                """
                SELECT company.company_name, version.source_batch_id
                FROM company
                JOIN tax_master_version AS version
                  ON version.company_id = company.id
                 AND version.status = 'PUBLISHED'
                WHERE company.id = :company_id
                ORDER BY version.id
                LIMIT 1
                """
            ),
            {"company_id": company_id},
        ).one()
        return connection.execute(
            text(
                """
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, source_file_name, source_checksum,
                    source_row_number, uploaded_by, data
                ) VALUES (
                    :company_id, :source_batch_id, DATE '2026-01-01', :version,
                    'DRAFT', 0.20, 0, 0.07, 'CNY', 2, 'overlap.xlsx',
                    :checksum, 99, 'overlap-maker',
                    jsonb_build_object(
                        'company_name', CAST(:company_name AS text)
                    )
                )
                RETURNING id
                """
            ),
            {
                "company_id": company_id,
                "source_batch_id": source_batch_id,
                "version": f"overlap-{token}",
                "checksum": token.ljust(64, "d")[:64],
                "company_name": company_name,
            },
        ).scalar_one()


def _coordinated_services(
    factory: sessionmaker[Session],
) -> tuple[TaxMasterService, SnapshotService, Event, Event, Event]:
    approval_company_locked = Event()
    allow_approval_overlap = Event()
    snapshot_company_attempted = Event()

    class ApprovalIngestRepository(IngestRepository):
        def lock_companies_exclusive(
            self,
            company_codes: Iterable[str],
        ):
            locked = super().lock_companies_exclusive(company_codes)
            approval_company_locked.set()
            if not allow_approval_overlap.wait(timeout=10):
                raise AssertionError("approval overlap was not released")
            return locked

    class ApprovalUnitOfWork(UnitOfWork):
        def __enter__(self):  # type: ignore[no-untyped-def]
            entered = super().__enter__()
            self.ingest = ApprovalIngestRepository(self.session)
            return entered

    class SnapshotIngestRepository(IngestRepository):
        def lock_companies_shared(
            self,
            company_codes: Iterable[str],
        ):
            snapshot_company_attempted.set()
            return super().lock_companies_shared(company_codes)

    class SnapshotUnitOfWork(UnitOfWork):
        def __enter__(self):  # type: ignore[no-untyped-def]
            entered = super().__enter__()
            self.ingest = SnapshotIngestRepository(self.session)
            return entered

    return (
        TaxMasterService(partial(ApprovalUnitOfWork, factory)),
        SnapshotService(partial(SnapshotUnitOfWork, factory)),
        approval_company_locked,
        allow_approval_overlap,
        snapshot_company_attempted,
    )


def _capture_outcome(callable_: object):
    assert callable(callable_)
    try:
        return callable_()
    except BaseException as error:  # outcomes are asserted by the concurrency contract
        return error


def test_company_master_ingest_and_snapshot_validation_have_no_batch_company_lock_cycle(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    _, engine, factory = service_resources
    company_code, _ = _seed_quality_case(engine)
    token = uuid4().hex
    with engine.begin() as connection:
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'COMPANY_REGISTRY', :key, 'company_master', 'RECEIVED', now(),
                    :period, 'FULL', '1', 'CNY', 2, 0, 0, 0, 0, repeat('0', 64)
                ) RETURNING id
                """
            ),
            {"key": f"lock-cycle-{token}", "period": PERIOD},
        ).scalar_one()

    ingest_company_attempted = Event()
    allow_ingest_company = Event()
    snapshot_batch_attempted = Event()

    class CoordinatedIngestRepository(IngestRepository):
        def lock_companies_exclusive(self, company_codes: Iterable[str]):
            ingest_company_attempted.set()
            if not allow_ingest_company.wait(timeout=10):
                raise AssertionError("snapshot did not reach its batch lock")
            return super().lock_companies_exclusive(company_codes)

    class CoordinatedIngestUnitOfWork(UnitOfWork):
        def __enter__(self):  # type: ignore[no-untyped-def]
            entered = super().__enter__()
            self.ingest = CoordinatedIngestRepository(self.session)
            return entered

    class CoordinatedSnapshotRepository(IngestRepository):
        def list_batches(
            self,
            batch_ids: Iterable[UUID],
            *,
            for_update: bool = False,
        ):
            if for_update:
                snapshot_batch_attempted.set()
            return super().list_batches(batch_ids, for_update=for_update)

    class CoordinatedSnapshotUnitOfWork(UnitOfWork):
        def __enter__(self):  # type: ignore[no-untyped-def]
            entered = super().__enter__()
            self.ingest = CoordinatedSnapshotRepository(self.session)
            return entered

    ingest_service = IngestService(partial(CoordinatedIngestUnitOfWork, factory))
    snapshot_service = SnapshotService(partial(CoordinatedSnapshotUnitOfWork, factory))
    payload = (
        "source_record_key,company_code,company_name,lifecycle,extracted_at\n"
        f"company-{token},{company_code},Updated Company,ACTIVE,"
        "2200-01-01T00:00:00+00:00\n"
    ).encode()

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload = executor.submit(
            _capture_outcome,
            lambda: ingest_service.ingest_csv(batch_id, "company.csv", payload),
        )
        assert ingest_company_attempted.wait(timeout=10)
        validation = executor.submit(
            _capture_outcome,
            lambda: snapshot_service.validate(
                company_code=company_code,
                period=PERIOD,
                source_batch_ids=(batch_id,),
            ),
        )
        assert snapshot_batch_attempted.wait(timeout=10)
        allow_ingest_company.set()
        upload_outcome = upload.result(timeout=15)
        validation_outcome = validation.result(timeout=15)

    assert isinstance(upload_outcome, BatchView), upload_outcome
    assert upload_outcome.status.value == "SUCCEEDED"
    assert not isinstance(validation_outcome, BaseException), validation_outcome
    assert validation_outcome.valid is False
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM ingest_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        ).scalar_one()
        audit_errors = connection.execute(
            text(
                """
                SELECT count(*) FROM ingest_error
                WHERE batch_id = :batch_id
                  AND error_code = 'INGEST_PROCESSING_FAILED'
                """
            ),
            {"batch_id": batch_id},
        ).scalar_one()
    assert status == "SUCCEEDED"
    assert audit_errors == 0


def test_snapshot_publish_and_overlapping_master_approval_have_no_lock_cycle(
    service_resources: tuple[SnapshotService, Engine, sessionmaker[Session]],
) -> None:
    service, engine, factory = service_resources
    _, _, snapshot_id = _draft_snapshot(service, engine)
    with engine.connect() as connection:
        company_id = connection.execute(
            text("SELECT company_id FROM accounting_snapshot WHERE id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one()
    candidate_id = _insert_overlapping_draft_master(engine, company_id)
    (
        approval_service,
        snapshot_service,
        approval_company_locked,
        allow_approval_overlap,
        snapshot_company_attempted,
    ) = _coordinated_services(factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval = executor.submit(
            _capture_outcome,
            lambda: approval_service.approve(
                candidate_id,
                reviewed_by="overlap-reviewer",
            ),
        )
        assert approval_company_locked.wait(timeout=10)
        publication = executor.submit(
            _capture_outcome,
            lambda: snapshot_service.publish(snapshot_id),
        )
        assert snapshot_company_attempted.wait(timeout=10)
        allow_approval_overlap.set()
        approval_outcome = approval.result(timeout=15)
        publication_outcome = publication.result(timeout=15)

    assert isinstance(publication_outcome, SnapshotView), publication_outcome
    assert publication_outcome.status == SnapshotStatus.PUBLISHED
    assert isinstance(approval_outcome, MasterDataConflictError), approval_outcome
    assert approval_outcome.error_code == "PUBLISHED_PERIOD_OVERLAP"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM tax_master_version WHERE id = :candidate_id"),
            {"candidate_id": candidate_id},
        ).scalar_one() == "DRAFT"


def test_snapshot_set_publish_and_overlapping_master_approval_have_no_lock_cycle(
    set_resources: tuple[
        SnapshotService,
        Engine,
        sessionmaker[Session],
        tuple[ExpectedSnapshotMember, ...],
        ExpectedSnapshotMember,
        dict[UUID, UUID],
    ],
) -> None:
    _, engine, factory, members, _, _ = set_resources
    candidate_id = _insert_overlapping_draft_master(
        engine,
        members[0].company_id,
    )
    set_key = _set_key("master-lock-order")
    (
        approval_service,
        snapshot_service,
        approval_company_locked,
        allow_approval_overlap,
        snapshot_company_attempted,
    ) = _coordinated_services(factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval = executor.submit(
            _capture_outcome,
            lambda: approval_service.approve(
                candidate_id,
                reviewed_by="overlap-reviewer",
            ),
        )
        assert approval_company_locked.wait(timeout=10)
        publication = executor.submit(
            _capture_outcome,
            lambda: snapshot_service.publish_set(
                set_key=set_key,
                period=PERIOD,
                expected_members=members[:100],
            ),
        )
        assert snapshot_company_attempted.wait(timeout=10)
        allow_approval_overlap.set()
        approval_outcome = approval.result(timeout=15)
        publication_outcome = publication.result(timeout=15)

    assert isinstance(publication_outcome, SnapshotSetView), publication_outcome
    assert publication_outcome.status == SnapshotSetStatus.PUBLISHED
    assert isinstance(approval_outcome, MasterDataConflictError), approval_outcome
    assert approval_outcome.error_code == "PUBLISHED_PERIOD_OVERLAP"
    with engine.connect() as connection:
        state = connection.execute(
            text(
                """
                SELECT version.status, snapshot_set.status
                FROM tax_master_version AS version
                CROSS JOIN snapshot_set
                WHERE version.id = :candidate_id
                  AND snapshot_set.id = :snapshot_set_id
                """
            ),
            {
                "candidate_id": candidate_id,
                "snapshot_set_id": publication_outcome.id,
            },
        ).one()
    assert state == ("DRAFT", "PUBLISHED")

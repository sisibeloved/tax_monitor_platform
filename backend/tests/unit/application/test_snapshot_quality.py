from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tax_risk.application.snapshots import (
    ExpectedSnapshotMember,
    MAX_SNAPSHOT_SET_MEMBERS,
    MAX_SNAPSHOT_SOURCE_BATCHES,
    REQUIRED_QUARTERLY_METRICS,
    SnapshotRequestError,
    SnapshotService,
    canonical_sha256,
    source_version_set_hash,
)


EXPECTED_METRICS = (
    "cumulative_profit",
    "received_dividends",
    "fair_value_change",
    "cumulative_revenue",
    "prior_quarter_current_tax",
    "current_quarter_current_tax",
    "other_payables_accrual",
    "hesi_no_invoice",
)


def test_required_quarterly_metrics_are_explicit_and_ordered() -> None:
    assert REQUIRED_QUARTERLY_METRICS == EXPECTED_METRICS


def test_canonical_hash_uses_decimal_strings_and_stable_mapping_order() -> None:
    left = canonical_sha256({"z": Decimal("-0.00"), "a": Decimal("1.2300")})
    right = canonical_sha256({"a": Decimal("1.2300"), "z": Decimal("-0.00")})

    assert left == right
    assert len(left) == 64


def test_source_version_hash_is_batch_order_independent_and_master_sensitive() -> None:
    batches = (
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "checksum": "b" * 64,
            "schema_version": "2",
            "partial_decision": {"accepted": False, "evidence": []},
        },
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "checksum": "a" * 64,
            "schema_version": "1",
            "partial_decision": {"accepted": True, "evidence": ["unrelated"]},
        },
    )
    master = {
        "id": "00000000-0000-0000-0000-000000000003",
        "version": "v1",
        "checksum": "c" * 64,
    }

    first = source_version_set_hash(batches, master)
    reordered = source_version_set_hash(tuple(reversed(batches)), master)
    changed_master = source_version_set_hash(batches, master | {"version": "v2"})

    assert first == reordered
    assert changed_master != first


@pytest.mark.parametrize("value", [0.1, float("nan"), float("inf")])
def test_canonical_hash_rejects_every_float(value: float) -> None:
    with pytest.raises(TypeError, match="float"):
        canonical_sha256({"amount": value})


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_canonical_hash_rejects_nonfinite_decimal(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        canonical_sha256({"amount": value})


def test_source_hash_changes_with_batch_checksum_and_partial_evidence() -> None:
    batch = {
        "id": "00000000-0000-0000-0000-000000000001",
        "checksum": "a" * 64,
        "schema_version": "1",
        "partial_decision": {
            "accepted": True,
            "evidence": [{"row_number": 2, "classification": "OTHER_COMPANY"}],
        },
    }
    master = {
        "id": "00000000-0000-0000-0000-000000000002",
        "version": "v1",
        "checksum": "m" * 64,
    }
    baseline = source_version_set_hash((batch,), master)

    checksum_changed = source_version_set_hash(
        (batch | {"checksum": "b" * 64},),
        master,
    )
    evidence_changed = source_version_set_hash(
        (
            batch
            | {
                "partial_decision": {
                    "accepted": True,
                    "evidence": [
                        {"row_number": 3, "classification": "NON_REQUIRED_METRIC"}
                    ],
                }
            },
        ),
        master,
    )

    assert checksum_changed != baseline
    assert evidence_changed != baseline


def test_full_snapshot_checksum_changes_with_metric_value() -> None:
    lineage = {
        "metrics": [
            {
                "metric_code": "cumulative_profit",
                "amount": Decimal("1.00"),
            }
        ]
    }

    assert canonical_sha256(lineage) != canonical_sha256(
        {
            "metrics": [
                {
                    "metric_code": "cumulative_profit",
                    "amount": Decimal("1.01"),
                }
            ]
        }
    )


def _unexpected_uow() -> object:
    raise AssertionError("oversized requests must be rejected before opening a unit of work")


def test_snapshot_service_rejects_oversized_source_selection_before_database() -> None:
    service = SnapshotService(_unexpected_uow)  # type: ignore[arg-type]
    source_ids = tuple(uuid4() for _ in range(MAX_SNAPSHOT_SOURCE_BATCHES + 1))

    with pytest.raises(SnapshotRequestError) as caught:
        service.validate(
            company_code="LIMIT-COMPANY",
            period=date(2026, 6, 30),
            source_batch_ids=source_ids,
        )

    assert caught.value.error_code == "SOURCE_BATCH_LIMIT_EXCEEDED"


def test_snapshot_service_rejects_oversized_partial_acceptance_before_database() -> None:
    service = SnapshotService(_unexpected_uow)  # type: ignore[arg-type]
    accepted_ids = tuple(uuid4() for _ in range(MAX_SNAPSHOT_SOURCE_BATCHES + 1))

    with pytest.raises(SnapshotRequestError) as caught:
        service.validate(
            company_code="LIMIT-COMPANY",
            period=date(2026, 6, 30),
            source_batch_ids=(uuid4(),),
            accepted_partial_batch_ids=accepted_ids,
        )

    assert caught.value.error_code == "PARTIAL_BATCH_LIMIT_EXCEEDED"


def test_snapshot_service_rejects_oversized_set_before_database() -> None:
    service = SnapshotService(_unexpected_uow)  # type: ignore[arg-type]
    members = tuple(
        ExpectedSnapshotMember(company_id=uuid4(), snapshot_id=uuid4())
        for _ in range(MAX_SNAPSHOT_SET_MEMBERS + 1)
    )

    with pytest.raises(SnapshotRequestError) as caught:
        service.publish_set(
            set_key="limit-set",
            period=date(2026, 6, 30),
            expected_members=members,
        )

    assert caught.value.error_code == "SNAPSHOT_SET_LIMIT_EXCEEDED"

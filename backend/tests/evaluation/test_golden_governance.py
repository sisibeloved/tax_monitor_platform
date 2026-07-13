from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tax_risk.application.business_entertainment.golden import (
    GoldenDatasetError,
    load_golden_dataset,
    validate_golden_records,
)


GOLDEN_PATH = (
    Path(__file__).parents[1] / "fixtures" / "business_entertainment" / "golden.jsonl"
)


def test_only_approved_frozen_dual_reviewed_records_enter_release_metrics() -> None:
    dataset = load_golden_dataset(GOLDEN_PATH)

    assert dataset.version == "business-entertainment-golden-v1"
    assert len(dataset.records) == 10
    assert len(dataset.frozen_version_checksum) == 64
    assert {record.source_mode for record in dataset.records} >= {
        "SAP_LINKED",
        "BUSINESS_DOCUMENT_UNLINKED",
        "SAP_COVERAGE_ONLY",
    }
    for record in dataset.records:
        assert record.approval_status == "APPROVED"
        assert record.freeze_status == "FROZEN"
        assert record.finance_annotation.annotator_id != record.tax_annotation.annotator_id
        assert record.adjudicator_id not in {
            record.finance_annotation.annotator_id,
            record.tax_annotation.annotator_id,
        }
        assert len(record.record_checksum) == 64


def test_frozen_record_checksum_rejects_mutation() -> None:
    dataset = load_golden_dataset(GOLDEN_PATH)
    mutated = [record.model_dump(mode="json") for record in dataset.records]
    changed = deepcopy(mutated)
    changed[0]["redacted_input"]["reason"] = "被冻结后被篡改"

    with pytest.raises(GoldenDatasetError, match="checksum"):
        validate_golden_records(changed)

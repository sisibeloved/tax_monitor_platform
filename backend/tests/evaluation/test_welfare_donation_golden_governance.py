from __future__ import annotations

import json
from pathlib import Path

import pytest

from tax_risk.application.semantic.evaluation import (
    GoldManifest,
    canonical_row_sha256,
    load_gold_rows,
    sha256_file,
)


GOLDEN_DIR = Path(__file__).parents[1] / "fixtures" / "golden"
REQUIRED_ZERO_MISS_TAGS = {
    "WELFARE_CUSTOMER_SUPPLIER_GOV_RECEPTION",
    "WELFARE_TRAINING_LECTURER_EXAM",
    "WELFARE_PROMOTIONAL_GIFT",
    "WELFARE_CUSTOMER_GIFT",
    "DONATION_SPONSORSHIP",
    "DONATION_NAMING_BRAND_EXPOSURE",
    "DONATION_ADVERTISING_RIGHTS",
}
EXPECTED_LABELS = {
    "welfare": {
        "BUSINESS_ENTERTAINMENT",
        "EMPLOYEE_EDUCATION",
        "ADVERTISING_PROMOTION",
        "CURRENT_ACCOUNT_REASONABLE",
        "INSUFFICIENT_EVIDENCE",
    },
    "donation": {
        "SPONSORSHIP",
        "ADVERTISING_PROMOTION",
        "CURRENT_ACCOUNT_REASONABLE",
        "INSUFFICIENT_EVIDENCE",
    },
}


@pytest.mark.parametrize("subject", ["welfare", "donation"])
def test_gold_set_is_large_dual_reviewed_and_label_complete(subject: str) -> None:
    rows = load_gold_rows(GOLDEN_DIR / f"{subject}.jsonl")

    assert len(rows) >= 50
    assert {row.subject for row in rows} == {subject.upper()}
    assert all(row.finance_review.role == "FINANCE" for row in rows)
    assert all(row.tax_review.role == "TAX" for row in rows)
    assert all(row.finance_review.reviewer_id != row.tax_review.reviewer_id for row in rows)
    assert all(
        row.adjudication.adjudicator_id
        not in {row.finance_review.reviewer_id, row.tax_review.reviewer_id}
        for row in rows
    )
    assert all(
        (row.expected_label, row.expected_risk) == (row.adjudication.label, row.adjudication.risk)
        for row in rows
    )
    assert EXPECTED_LABELS[subject].issubset({row.expected_label for row in rows})
    assert any(
        (row.finance_review.label, row.finance_review.risk)
        != (row.tax_review.label, row.tax_review.risk)
        for row in rows
    )
    assert any("REVERSAL" in row.case_tags for row in rows)
    assert any("AMBIGUOUS" in row.case_tags for row in rows)


def test_approved_frozen_checksums_are_reproducible() -> None:
    manifest = GoldManifest.model_validate_json(
        (GOLDEN_DIR / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest.status == "APPROVED"
    assert manifest.frozen is True
    assert len(manifest.files) == 2
    for entry in manifest.files:
        path = GOLDEN_DIR / entry.path
        rows = load_gold_rows(path)
        assert entry.status == "APPROVED" and entry.frozen is True
        assert entry.sha256 == sha256_file(path)
        assert entry.row_count == len(rows)
        assert all(row.gold_set_version == entry.gold_set_version for row in rows)
        assert all(row.row_checksum == canonical_row_sha256(row) for row in rows)


def test_required_typical_cases_are_approved_and_sap_only() -> None:
    raw_rows = [
        json.loads(line)
        for name in ("welfare.jsonl", "donation.jsonl")
        for line in (GOLDEN_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tags = {tag for row in raw_rows for tag in row["case_tags"]}

    assert REQUIRED_ZERO_MISS_TAGS.issubset(tags)
    assert all(row["approval_status"] == "APPROVED" and row["frozen"] for row in raw_rows)
    assert all(
        row["expected_risk"] for row in raw_rows if REQUIRED_ZERO_MISS_TAGS & set(row["case_tags"])
    )
    assert all(
        not ({"oa_record_id", "hesi_record_id", "application_reason"} & row.keys())
        for row in raw_rows
    )

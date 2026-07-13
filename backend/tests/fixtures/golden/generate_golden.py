"""Generate the frozen, governed phase-3 golden fixtures deterministically."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tax_risk.application.semantic.evaluation import (
    GoldFileManifest,
    GoldManifest,
    GoldRow,
    canonical_row_sha256,
    sha256_file,
)


ROOT = Path(__file__).parent
APPROVED_AT = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
FROZEN_AT = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)


WELFARE_CASES = (
    ("客户商务宴请", ("WELFARE_CUSTOMER_SUPPLIER_GOV_RECEPTION",), "BUSINESS_ENTERTAINMENT", True),
    (
        "供应商接待用餐",
        ("WELFARE_CUSTOMER_SUPPLIER_GOV_RECEPTION",),
        "BUSINESS_ENTERTAINMENT",
        True,
    ),
    ("员工培训费", ("WELFARE_TRAINING_LECTURER_EXAM",), "EMPLOYEE_EDUCATION", True),
    ("内部讲师费", ("WELFARE_TRAINING_LECTURER_EXAM",), "EMPLOYEE_EDUCATION", True),
    ("市场宣传赠品", ("WELFARE_PROMOTIONAL_GIFT",), "ADVERTISING_PROMOTION", True),
    ("客户礼品", ("WELFARE_CUSTOMER_GIFT", "AMBIGUOUS"), "BUSINESS_ENTERTAINMENT", True),
    ("员工年度体检", ("WELFARE_REASONABLE_EMPLOYEE_BENEFIT",), "CURRENT_ACCOUNT_REASONABLE", False),
    (
        "福利事项证据不足材料不足",
        ("WELFARE_INSUFFICIENT_EVIDENCE",),
        "INSUFFICIENT_EVIDENCE",
        False,
    ),
    (
        "员工体检冲销",
        ("WELFARE_REASONABLE_EMPLOYEE_BENEFIT", "REVERSAL"),
        "CURRENT_ACCOUNT_REASONABLE",
        False,
    ),
    ("员工考试费", ("WELFARE_TRAINING_LECTURER_EXAM", "AMBIGUOUS"), "EMPLOYEE_EDUCATION", True),
)

DONATION_CASES = (
    ("公益活动赞助", ("DONATION_SPONSORSHIP",), "SPONSORSHIP", True),
    ("公益活动冠名", ("DONATION_NAMING_BRAND_EXPOSURE",), "ADVERTISING_PROMOTION", True),
    ("公益项目品牌露出", ("DONATION_NAMING_BRAND_EXPOSURE",), "ADVERTISING_PROMOTION", True),
    ("公益合作含广告权益", ("DONATION_ADVERTISING_RIGHTS",), "ADVERTISING_PROMOTION", True),
    ("赛事公益赞助", ("DONATION_SPONSORSHIP",), "SPONSORSHIP", True),
    (
        "无对价公益捐赠且材料完整",
        ("DONATION_REASONABLE_NO_CONSIDERATION",),
        "CURRENT_ACCOUNT_REASONABLE",
        False,
    ),
    (
        "公益捐赠证据不足材料不足",
        ("DONATION_INSUFFICIENT_EVIDENCE",),
        "INSUFFICIENT_EVIDENCE",
        False,
    ),
    (
        "无对价公益捐赠冲销",
        ("DONATION_REASONABLE_NO_CONSIDERATION", "REVERSAL"),
        "CURRENT_ACCOUNT_REASONABLE",
        False,
    ),
    (
        "联合公益活动冠名",
        ("DONATION_NAMING_BRAND_EXPOSURE", "AMBIGUOUS"),
        "ADVERTISING_PROMOTION",
        True,
    ),
    (
        "定向无对价公益捐赠",
        ("DONATION_REASONABLE_NO_CONSIDERATION",),
        "CURRENT_ACCOUNT_REASONABLE",
        False,
    ),
)


def main() -> None:
    entries: list[GoldFileManifest] = []
    for subject, cases in (("WELFARE", WELFARE_CASES), ("DONATION", DONATION_CASES)):
        version = f"{subject.lower()}-gold-v1"
        rows = [
            _build_row(subject, version, index, cases[index % len(cases)]) for index in range(50)
        ]
        path = ROOT / f"{subject.lower()}.jsonl"
        path.write_text(
            "".join(
                json.dumps(
                    row.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        entries.append(
            GoldFileManifest(
                path=path.name,
                sha256=sha256_file(path),
                row_count=len(rows),
                gold_set_version=version,
                status="APPROVED",
                frozen=True,
                approved_by="gold-owner",
                approved_at=APPROVED_AT,
            )
        )
    manifest = GoldManifest(
        status="APPROVED",
        frozen=True,
        approved_by="gold-owner",
        approved_at=APPROVED_AT,
        files=(entries[0], entries[1]),
    )
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _build_row(
    subject: str,
    version: str,
    index: int,
    case: tuple[str, tuple[str, ...], str, bool],
) -> GoldRow:
    summary, tags, expected_label, expected_risk = case
    row_id = f"{subject[0].lower()}-{index + 1:03d}"
    finance_label = expected_label
    finance_risk = expected_risk
    if "AMBIGUOUS" in tags:
        finance_label = (
            "ADVERTISING_PROMOTION" if expected_label != "ADVERTISING_PROMOTION" else "SPONSORSHIP"
        )
        finance_risk = True
    amount = Decimal("-500.00") if "REVERSAL" in tags else Decimal(index + 1) * 100
    row = GoldRow.model_validate(
        {
            "id": row_id,
            "subject": subject,
            "company_code": "1001" if subject == "WELFARE" else "1002",
            "period": "2026-06",
            "sap_fiscal_year": 2026,
            "voucher_no": f"{(510000 if subject == 'WELFARE' else 610000) + index + 1}",
            "line_item_no": f"{index + 1:03d}",
            "current_account": "职工福利费" if subject == "WELFARE" else "公益性捐赠",
            "amount": amount,
            "currency": "CNY",
            "summary": f"{summary}（样本{index + 1:02d}）",
            "case_tags": tags,
            "expected_label": expected_label,
            "expected_risk": expected_risk,
            "finance_review": {
                "role": "FINANCE",
                "reviewer_id": "finance-02",
                "label": finance_label,
                "risk": finance_risk,
                "reviewed_at": "2026-06-28T09:00:00Z",
            },
            "tax_review": {
                "role": "TAX",
                "reviewer_id": "tax-01",
                "label": expected_label,
                "risk": expected_risk,
                "reviewed_at": "2026-06-28T10:00:00Z",
            },
            "adjudication": {
                "adjudicator_id": "tax-lead",
                "label": expected_label,
                "risk": expected_risk,
                "adjudicated_at": "2026-06-29T09:00:00Z",
            },
            "gold_set_version": version,
            "approval_status": "APPROVED",
            "approved_by": "gold-owner",
            "approved_at": APPROVED_AT,
            "frozen": True,
            "frozen_at": FROZEN_AT,
            "row_checksum": "0" * 64,
        }
    )
    return row.model_copy(update={"row_checksum": canonical_row_sha256(row)})


if __name__ == "__main__":
    main()

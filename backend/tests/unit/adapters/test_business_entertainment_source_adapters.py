from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest

from tax_risk.adapters.ingest.csv_adapter import HeaderValidationError
from tax_risk.adapters.ingest.hesi_business_entertainment_csv import (
    HesiBusinessEntertainmentCsvAdapter,
)
from tax_risk.adapters.ingest.oa_business_entertainment_csv import (
    OaBusinessEntertainmentCsvAdapter,
)
from tax_risk.adapters.ingest.oa_material_requisition_csv import (
    OaMaterialRequisitionCsvAdapter,
)
from tax_risk.adapters.ingest.oa_self_procurement_csv import (
    OaSelfProcurementCsvAdapter,
)
from tax_risk.adapters.ingest.sap_business_entertainment_csv import (
    SapBusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.business_entertainment.source_models import (
    HesiBusinessEntertainmentRecord,
    OaBusinessEntertainmentRecord,
    OaMaterialRequisitionRecord,
    OaSelfProcurementRecord,
)
from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SapExpenseVoucherRecord,
)


def _csv(headers: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


@pytest.mark.parametrize(
    ("adapter", "row", "expected_type", "expected_key"),
    [
        (
            SapBusinessEntertainmentCsvAdapter,
            {
                "company_code": "C001", "fiscal_year": 2026, "period": 3,
                "posting_date": "2026-03-18", "document_number": "510001",
                "line_item": "001", "current_account_code": "660201",
                "current_account_name": "业务招待费", "amount": "-120.50",
                "currency": "CNY", "summary": "冲销客户餐费", "assignment": "OA-1",
                "reference": "BX-1", "reversal_reference": "510000",
            },
            SapExpenseVoucherRecord,
            "C001|2026|510001|001",
        ),
        (
            HesiBusinessEntertainmentCsvAdapter,
            {
                "company_code": "C001", "fiscal_year": 2026, "period": 3,
                "expense_claim_id": "BX-1", "line_id": "1", "expense_date": "2026-03-17",
                "amount": "120.50", "currency": "CNY", "summary": "客户餐费",
                "expense_reason": "客户来访", "recipient_category": "客户",
                "participant_count": 4, "related_oa_id": "OA-1",
                "sap_document_number": "510001", "sap_line_item": "001",
            },
            HesiBusinessEntertainmentRecord,
            "C001|BX-1|1",
        ),
        (
            OaBusinessEntertainmentCsvAdapter,
            {
                "company_code": "C001", "application_id": "OA-1", "line_id": "1",
                "application_date": "2026-03-10", "reason": "客户来访",
                "recipient_category": "客户", "participant_count": 4,
                "amount": "120.50", "currency": "CNY",
            },
            OaBusinessEntertainmentRecord,
            "C001|OA-1|1",
        ),
        (
            OaSelfProcurementCsvAdapter,
            {
                "company_code": "C001", "application_id": "ZC-1", "line_id": "1",
                "purchase_date": "2026-03-11", "item_description": "伴手礼",
                "reason": "客户来访", "recipient_category": "客户", "amount": "88.00",
                "currency": "CNY", "parent_oa_id": "OA-1", "parent_hesi_id": "BX-1",
            },
            OaSelfProcurementRecord,
            "C001|ZC-1|1",
        ),
        (
            OaMaterialRequisitionCsvAdapter,
            {
                "company_code": "C001", "requisition_id": "WL-1", "line_id": "1",
                "requisition_date": "2026-03-12", "material_description": "礼盒",
                "purpose": "客户来访", "recipient_category": "客户", "quantity": "2.000",
                "unit": "盒", "amount": "100.00", "currency": "CNY",
                "parent_oa_id": "OA-1", "parent_hesi_id": "BX-1",
            },
            OaMaterialRequisitionRecord,
            "C001|WL-1|1",
        ),
    ],
)
def test_each_source_adapter_yields_strict_decimal_record(
    adapter: type,
    row: dict[str, object],
    expected_type: type,
    expected_key: str,
) -> None:
    payload = _csv(adapter.HEADER, [row])

    result = list(adapter(payload).iter_rows())

    assert len(result) == 1
    assert result[0].error is None
    assert isinstance(result[0].value, expected_type)
    assert result[0].value.source_record_key == expected_key
    if hasattr(result[0].value, "amount"):
        assert isinstance(result[0].value.amount, Decimal)


def test_sap_reversal_amount_is_negative_and_account_family_is_server_controlled() -> None:
    row = {
        "company_code": "C001", "fiscal_year": 2026, "period": 3,
        "posting_date": "2026-03-18", "document_number": "510001", "line_item": "001",
        "current_account_code": "660201", "current_account_name": "业务招待费",
        "amount": "-120.50", "currency": "CNY", "summary": "冲销客户餐费",
        "assignment": "", "reference": "", "reversal_reference": "510000",
    }

    parsed = next(SapBusinessEntertainmentCsvAdapter(_csv(SapBusinessEntertainmentCsvAdapter.HEADER, [row])).iter_rows()).value

    assert isinstance(parsed, SapExpenseVoucherRecord)
    assert parsed.amount == Decimal("-120.50")
    assert parsed.account_family == AccountFamily.BUSINESS_ENTERTAINMENT
    assert parsed.posting_date == date(2026, 3, 18)


def test_pii_or_unknown_header_is_rejected() -> None:
    headers = SapBusinessEntertainmentCsvAdapter.HEADER + ("participant_names",)
    adapter = SapBusinessEntertainmentCsvAdapter(_csv(headers, []))

    with pytest.raises(HeaderValidationError) as captured:
        adapter.validate_header()

    assert captured.value.error_code == "INVALID_HEADER"
    assert captured.value.extra_columns == ("participant_names",)


def test_duplicate_source_key_and_invalid_decimal_are_row_errors() -> None:
    base = {
        "company_code": "C001", "application_id": "OA-1", "line_id": "1",
        "application_date": "2026-03-10", "reason": "客户来访",
        "recipient_category": "客户", "participant_count": 4,
        "amount": "120.50", "currency": "CNY",
    }
    rows = [base, dict(base), {**base, "line_id": "2", "amount": "not-a-decimal"}]

    parsed = list(OaBusinessEntertainmentCsvAdapter(_csv(OaBusinessEntertainmentCsvAdapter.HEADER, rows)).iter_rows())

    assert parsed[0].error is None
    assert parsed[1].error is not None
    assert parsed[1].error.error_code == "DUPLICATE_SOURCE_RECORD_KEY"
    assert parsed[2].error is not None
    assert parsed[2].error.error_code == "INVALID_DECIMAL"

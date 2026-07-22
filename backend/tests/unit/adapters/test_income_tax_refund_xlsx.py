from __future__ import annotations

from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from tax_risk.adapters.ingest.income_tax_refund_xlsx import (
    IncomeTaxRefundWorkbookError,
    IncomeTaxRefundXlsxAdapter,
)
from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits


def _workbook_bytes(rows: list[list[object]], *, year: int = 2025) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "法人主体所得税税负率&利润率等"
    worksheet.append(
        [
            "统一信用代码",
            "公司代码",
            "公司名称",
            f"{year}年是否涉及退税",
            f"{year}年应退税金额",
            "是否已收到退税",
        ]
    )
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_parser_selects_refund_rows_and_rounds_at_explicit_currency_scale() -> None:
    payload = _workbook_bytes(
        [
            ["credit-1", 3000, "Company One", "是", 31.4375, ""],
            ["credit-2", "3560", "Company Two", "否", 100, ""],
            ["credit-3", "4000", "Company Three", "是", "797109.5125", "已退税"],
        ]
    )

    rows = IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2025).parse()

    assert [row.company_code for row in rows] == ["3000", "4000"]
    assert str(rows[0].raw_expected_refund_amount) == "31.4375"
    assert str(rows[0].expected_refund_amount) == "31.44"
    assert str(rows[1].expected_refund_amount) == "797109.51"
    assert rows[0].source_record_key == "credit-1|3000|2025"
    assert rows[1].received_in_source is True


def test_parser_normalizes_excel_formula_float_noise_before_half_up_rounding() -> None:
    payload = _workbook_bytes(
        [["credit-1", "3000", "Company One", "是", 57.5049999999756, ""]]
    )

    rows = IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2025).parse()

    assert rows[0].raw_expected_refund_amount == Decimal("57.5050")
    assert rows[0].expected_refund_amount == Decimal("57.51")


def test_parser_locates_dynamic_year_columns() -> None:
    payload = _workbook_bytes(
        [["credit-1", "3000", "Company One", "是", "10.00", "未退税"]],
        year=2026,
    )

    rows = IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2026).parse()

    assert rows[0].refund_tax_year == 2026


def test_parser_rejects_duplicate_company_targets() -> None:
    payload = _workbook_bytes(
        [
            ["credit-1", "3000", "Company One", "是", "10.00", ""],
            ["credit-1", "3000", "Company One", "是", "20.00", ""],
        ]
    )

    with pytest.raises(IncomeTaxRefundWorkbookError) as captured:
        IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2025).parse()

    assert {error.error_code for error in captured.value.errors} == {
        "DUPLICATE_COMPANY_REFUND"
    }


@pytest.mark.parametrize("status", ["处理中", "收到"])
def test_parser_rejects_uncontrolled_receipt_status(status: str) -> None:
    payload = _workbook_bytes(
        [["credit-1", "3000", "Company One", "是", "10.00", status]]
    )

    with pytest.raises(IncomeTaxRefundWorkbookError) as captured:
        IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2025).parse()

    assert captured.value.errors[0].error_code == "INVALID_RECEIPT_STATUS"


def test_parser_rejects_missing_year_specific_columns() -> None:
    payload = _workbook_bytes(
        [["credit-1", "3000", "Company One", "是", "10.00", ""]],
        year=2024,
    )

    with pytest.raises(IncomeTaxRefundWorkbookError) as captured:
        IncomeTaxRefundXlsxAdapter(payload, refund_tax_year=2025).parse()

    assert captured.value.errors[0].error_code == "INVALID_HEADER"


def test_parser_applies_shared_xlsx_resource_preflight() -> None:
    payload = _workbook_bytes(
        [["credit-1", "3000", "Company One", "是", "10.00", ""]]
    )
    limits = XlsxResourceLimits(max_zip_members=1)

    with pytest.raises(IncomeTaxRefundWorkbookError) as captured:
        IncomeTaxRefundXlsxAdapter(
            payload,
            refund_tax_year=2025,
            limits=limits,
        ).parse()

    assert captured.value.errors[0].error_code == "XLSX_RESOURCE_LIMIT_EXCEEDED"

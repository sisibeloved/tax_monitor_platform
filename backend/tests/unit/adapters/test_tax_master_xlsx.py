from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from tax_risk.adapters.ingest.tax_master_xlsx import (
    TaxMasterWorkbookError,
    TaxMasterXlsxAdapter,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HEADERS = (
    "company_code",
    "company_name",
    "valid_from",
    "valid_to",
    "tax_rate",
    "loss_carryforward",
    "three_year_average_tax_burden",
)


def _xlsx(
    rows: list[tuple[object, ...]],
    *,
    headers: tuple[object, ...] = HEADERS,
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "tax_master"
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _valid_row(**overrides: object) -> tuple[object, ...]:
    values: dict[str, object] = {
        "company_code": "C001",
        "company_name": "Company One",
        "valid_from": date(2026, 1, 1),
        "valid_to": None,
        "tax_rate": "25%",
        "loss_carryforward": "100000.00",
        "three_year_average_tax_burden": "9%",
    }
    values.update(overrides)
    return tuple(values[column] for column in HEADERS)


def test_parses_controlled_fixture_and_normalizes_displayed_rates_exactly() -> None:
    payload = (FIXTURES / "tax_master_valid.xlsx").read_bytes()

    parsed = TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert len(parsed) == 2
    assert parsed[0].row_number == 2
    assert parsed[0].tax_rate.value == Decimal("0.25")
    assert parsed[0].three_year_average_tax_burden.value == Decimal("0.09")
    assert parsed[0].loss_carryforward == Decimal("100000.00")
    assert parsed[0].valid_to is None
    # The Excel numeric cell is read as a float, but is normalized through str(value),
    # never passed directly into Rate or Decimal as a binary float.
    assert parsed[1].tax_rate.value == Decimal("0.25")
    assert parsed[1].three_year_average_tax_burden.value == Decimal("0.08")
    assert parsed[1].valid_to == date(2026, 12, 31)
    assert len(TaxMasterXlsxAdapter(payload, amount_scale=2).checksum) == 64


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        (HEADERS + ("unexpected",), "INVALID_HEADER"),
        (HEADERS[:-1] + (None,), "INVALID_HEADER"),
        (HEADERS[:-1], "INVALID_HEADER"),
    ],
)
def test_rejects_missing_blank_or_extra_headers(
    headers: tuple[object, ...],
    expected_code: str,
) -> None:
    payload = _xlsx([_valid_row()[: len(headers)]], headers=headers)

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert caught.value.errors[0].error_code == expected_code
    assert caught.value.errors[0].row_number == 1


def test_rejects_formula_without_cached_value_and_interior_blank_row() -> None:
    formula_payload = _xlsx([_valid_row(tax_rate="=1/4")])
    blank_payload = _xlsx(
        [_valid_row(), (None,) * len(HEADERS), _valid_row(company_code="C002")]
    )

    with pytest.raises(TaxMasterWorkbookError) as formula_error:
        TaxMasterXlsxAdapter(formula_payload, amount_scale=2).parse()
    with pytest.raises(TaxMasterWorkbookError) as blank_error:
        TaxMasterXlsxAdapter(blank_payload, amount_scale=2).parse()

    assert [(error.row_number, error.error_code) for error in formula_error.value.errors] == [
        (2, "FORMULA_WITHOUT_CACHED_VALUE")
    ]
    assert formula_error.value.record_count == 1
    assert formula_error.value.loss_control_total == Decimal("100000.00")
    assert [(error.row_number, error.error_code) for error in blank_error.value.errors] == [
        (3, "BLANK_ROW")
    ]


@pytest.mark.parametrize(
    ("overrides", "expected_code", "field"),
    [
        ({"tax_rate": "101%"}, "INVALID_RATE", "tax_rate"),
        ({"tax_rate": "-1%"}, "INVALID_RATE", "tax_rate"),
        ({"tax_rate": "0.1234567890123"}, "INVALID_RATE", "tax_rate"),
        ({"three_year_average_tax_burden": "1.01"}, "INVALID_RATE", "three_year_average_tax_burden"),
        ({"loss_carryforward": "-0.01"}, "INVALID_LOSS_CARRYFORWARD", "loss_carryforward"),
        ({"loss_carryforward": "0.001"}, "INVALID_LOSS_CARRYFORWARD", "loss_carryforward"),
        ({"loss_carryforward": "1E+40"}, "INVALID_LOSS_CARRYFORWARD", "loss_carryforward"),
        ({"valid_from": "2026/01/01"}, "INVALID_DATE", "valid_from"),
        ({"valid_to": date(2025, 12, 31)}, "INVALID_EFFECTIVE_PERIOD", "valid_to"),
    ],
)
def test_rejects_invalid_values_without_coercion(
    overrides: dict[str, object],
    expected_code: str,
    field: str,
) -> None:
    payload = _xlsx([_valid_row(**overrides)])

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert caught.value.errors[0].error_code == expected_code
    assert caught.value.errors[0].field == field


def test_duplicate_and_overlapping_company_periods_reject_the_whole_file_stably() -> None:
    payload = (FIXTURES / "tax_master_duplicate.xlsx").read_bytes()

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert [(error.row_number, error.error_code) for error in caught.value.errors] == [
        (3, "OVERLAPPING_EFFECTIVE_PERIOD")
    ]
    assert caught.value.record_count == 2
    assert len(caught.value.valid_rows) == 2
    assert caught.value.loss_control_total == Decimal("0")


def test_empty_workbook_is_rejected_but_trailing_blank_rows_are_ignored() -> None:
    empty = _xlsx([])
    trailing_blank = _xlsx([_valid_row(), (None,) * len(HEADERS)])

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(empty, amount_scale=2).parse()

    assert caught.value.errors[0].error_code == "EMPTY_FILE"
    assert len(TaxMasterXlsxAdapter(trailing_blank, amount_scale=2).parse()) == 1


def test_unreadable_loss_marks_control_total_unavailable() -> None:
    payload = _xlsx([_valid_row(loss_carryforward="not-a-decimal")])

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert caught.value.record_count == 1
    assert caught.value.loss_control_total is None
    assert caught.value.errors[0].error_code == "INVALID_LOSS_CARRYFORWARD"


def test_invalid_xlsx_reports_file_level_metadata_for_failed_batch_audit() -> None:
    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(b"not-an-xlsx", amount_scale=2).parse()

    assert caught.value.record_count == 0
    assert caught.value.valid_rows == ()
    assert caught.value.loss_control_total is None
    assert caught.value.errors[0].error_code == "INVALID_XLSX"


def test_excel_numeric_with_more_than_fifteen_significant_digits_is_rejected() -> None:
    exact_value = Decimal("12345678901234567890123456.12")
    payload = _xlsx([_valid_row(loss_carryforward=float(exact_value))])

    with pytest.raises(TaxMasterWorkbookError) as caught:
        TaxMasterXlsxAdapter(payload, amount_scale=2).parse()

    assert caught.value.errors[0].error_code == "EXCEL_NUMERIC_PRECISION_EXCEEDED"
    assert caught.value.errors[0].field == "loss_carryforward"
    assert "text" in caught.value.errors[0].message.lower()

from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook

from tax_risk.adapters.ingest.business_entertainment_company_list_xlsx import (
    BusinessEntertainmentScopeWorkbookError,
    BusinessEntertainmentScopeXlsxAdapter,
)


HEADERS = ("company_code", "effective_from", "effective_to")


def _xlsx(
    rows: list[tuple[object, object, object]],
    *,
    headers: tuple[str, ...] = HEADERS,
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "business_entertainment_scope"
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_adapter_parses_the_exact_scope_contract() -> None:
    rows = BusinessEntertainmentScopeXlsxAdapter(
        _xlsx(
            [
                (" 1001 ", date(2026, 1, 1), date(2026, 12, 31)),
                ("1002", "2026-01-01", "2026-12-31"),
            ]
        )
    ).parse()

    assert [(row.row_number, row.company_code) for row in rows] == [
        (2, "1001"),
        (3, "1002"),
    ]
    assert rows[0].effective_from == date(2026, 1, 1)
    assert rows[0].effective_to == date(2026, 12, 31)


@pytest.mark.parametrize(
    ("rows", "error_code", "field"),
    [
        ([('', '2026-01-01', '2026-12-31')], "REQUIRED_VALUE", "company_code"),
        (
            [("1001", "2026-12-31", "2026-01-01")],
            "INVALID_EFFECTIVE_PERIOD",
            "effective_to",
        ),
        (
            [
                ("1001", "2026-01-01", "2026-12-31"),
                ("1001", "2026-01-01", "2026-12-31"),
            ],
            "DUPLICATE_COMPANY",
            "company_code",
        ),
    ],
)
def test_adapter_rejects_invalid_scope_rows(
    rows: list[tuple[object, object, object]],
    error_code: str,
    field: str,
) -> None:
    with pytest.raises(BusinessEntertainmentScopeWorkbookError) as captured:
        BusinessEntertainmentScopeXlsxAdapter(_xlsx(rows)).parse()

    assert any(
        issue.error_code == error_code and issue.field == field
        for issue in captured.value.errors
    )


def test_adapter_rejects_any_header_drift() -> None:
    with pytest.raises(BusinessEntertainmentScopeWorkbookError) as captured:
        BusinessEntertainmentScopeXlsxAdapter(
            _xlsx(
                [("1001", "2026-01-01", "2026-12-31")],
                headers=("company_code", "effective_from", "end_date"),
            )
        ).parse()

    assert captured.value.errors[0].error_code == "INVALID_HEADER"

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    BulkFileAdapter,
    CanonicalFinancialRow,
    CompanyMasterRow,
)
from tax_risk.adapters.ingest.csv_adapter import CSVAdapter, HeaderValidationError


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_valid_financial_csv_yields_canonical_rows_and_hashes_raw_file() -> None:
    payload = (FIXTURES / "sap_quarterly_valid.csv").read_bytes()
    adapter: BulkFileAdapter = CSVAdapter(payload, dataset_code="quarterly_metric")

    adapter.validate_header()
    parsed = list(adapter.iter_rows())

    assert adapter.checksum == sha256(payload).hexdigest()
    assert [item.row_number for item in parsed] == [2, 3, 4]
    assert all(item.error is None for item in parsed)
    rows = [item.value for item in parsed]
    assert rows[0] == CanonicalFinancialRow(
        source_record_key="sap-q1-001",
        company_code="C001",
        fiscal_year=2026,
        period=date(2026, 3, 31),
        currency="CNY",
        amount_scale=2,
        metric_code="cumulative_profit",
        amount=Decimal("100000.00"),
        extracted_at=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
    )
    assert isinstance(rows[1], CanonicalFinancialRow)
    assert rows[1].amount == Decimal("-500.00")


def test_invalid_decimal_is_a_row_error_and_is_never_coerced_to_zero() -> None:
    payload = (FIXTURES / "sap_quarterly_invalid.csv").read_bytes()
    adapter = CSVAdapter(payload, dataset_code="quarterly_metric")

    parsed = list(adapter.iter_rows())

    assert len(parsed) == 4
    invalid = parsed[2]
    assert invalid == AdapterRow(
        row_number=4,
        value=None,
        error=invalid.error,
    )
    assert invalid.error is not None
    assert invalid.error.error_code == "INVALID_DECIMAL"
    assert invalid.error.field == "amount"
    assert invalid.error.rejected_value == "not-a-decimal"
    assert invalid.error.context == (
        ("company_code", "C001"),
        ("metric_code", "fair_value_change"),
    )
    assert all(
        not (
            isinstance(item.value, CanonicalFinancialRow)
            and item.value.source_record_key == "sap-bad-003"
            and item.value.amount == Decimal("0")
        )
        for item in parsed
    )


def test_blank_amount_is_rejected_instead_of_becoming_zero() -> None:
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "blank-1,C001,2026,2026-03-31,CNY,2,cumulative_profit,,"
        "2026-04-01T08:00:00+00:00\n"
    ).encode()

    result = next(CSVAdapter(payload, dataset_code="quarterly_metric").iter_rows())

    assert result.value is None
    assert result.error is not None
    assert result.error.error_code == "MISSING_VALUE"
    assert result.error.field == "amount"


@pytest.mark.parametrize(
    ("company_code", "metric_code", "expected_context"),
    [
        ("   ", "cumulative_profit", (("metric_code", "cumulative_profit"),)),
        ("C" * 65, "cumulative_profit", (("metric_code", "cumulative_profit"),)),
        ("C001", "M" * 129, (("company_code", "C001"),)),
    ],
)
def test_invalid_row_context_omits_blank_or_overlong_identity_values(
    company_code: str,
    metric_code: str,
    expected_context: tuple[tuple[str, str], ...],
) -> None:
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        f"bad-context,{company_code},2026,2026-03-31,CNY,2,{metric_code},bad,"
        "2026-04-01T08:00:00+00:00\n"
    ).encode()

    result = next(CSVAdapter(payload, dataset_code="quarterly_metric").iter_rows())

    assert result.error is not None
    assert result.error.context == expected_context


def test_financial_row_rejects_non_decimal_amounts() -> None:
    with pytest.raises(TypeError, match="amount must be Decimal"):
        CanonicalFinancialRow(
            source_record_key="float-1",
            company_code="C001",
            fiscal_year=2026,
            period=date(2026, 3, 31),
            currency="CNY",
            amount_scale=2,
            metric_code="cumulative_profit",
            amount=1.25,  # type: ignore[arg-type]
            extracted_at=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
        )


def test_company_master_schema_is_explicit_and_yields_lifecycle_rows() -> None:
    payload = (
        "source_record_key,company_code,company_name,lifecycle,extracted_at\n"
        "master-1,C001,Company One,ACTIVE,2026-04-01T08:00:00+00:00\n"
        "master-2,C002,Company Two,INACTIVE,2026-04-01T09:00:00+00:00\n"
    ).encode()
    adapter = CSVAdapter(payload, dataset_code="company_master")

    adapter.validate_header()
    parsed = list(adapter.iter_rows())

    assert [item.value for item in parsed] == [
        CompanyMasterRow(
            source_record_key="master-1",
            company_code="C001",
            company_name="Company One",
            lifecycle="ACTIVE",
            extracted_at=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
        ),
        CompanyMasterRow(
            source_record_key="master-2",
            company_code="C002",
            company_name="Company Two",
            lifecycle="INACTIVE",
            extracted_at=datetime(2026, 4, 1, 9, tzinfo=timezone.utc),
        ),
    ]


def test_header_validation_rejects_missing_or_extra_columns() -> None:
    payload = b"source_record_key,company_code,amount,unexpected\nrow-1,C001,10.00,value\n"

    with pytest.raises(HeaderValidationError) as raised:
        CSVAdapter(payload, dataset_code="quarterly_metric").validate_header()

    assert raised.value.error_code == "INVALID_HEADER"
    assert "fiscal_year" in raised.value.missing_columns
    assert raised.value.extra_columns == ("unexpected",)


def test_unsupported_dataset_has_a_stable_header_error() -> None:
    adapter = CSVAdapter(b"anything\nvalue\n", dataset_code="unknown_dataset")

    with pytest.raises(HeaderValidationError) as raised:
        adapter.validate_header()

    assert raised.value.error_code == "UNSUPPORTED_DATASET"


def test_row_errors_use_physical_csv_line_numbers_after_blank_lines() -> None:
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "\n"
        "line-3,C001,2026,2026-03-31,CNY,2,cumulative_profit,bad,"
        "2026-04-01T08:00:00+00:00\n"
    ).encode()

    result = next(CSVAdapter(payload, dataset_code="quarterly_metric").iter_rows())

    assert result.row_number == 3
    assert result.error is not None
    assert result.error.row_number == 3


@pytest.mark.parametrize(
    "data_row",
    [
        (
            "wide-1,C001,2026,2026-03-31,CNY,2,cumulative_profit,10.00,"
            "2026-04-01T08:00:00+00:00,unexpected"
        ),
        "short-1,C001,2026,2026-03-31,CNY,2,cumulative_profit,10.00",
    ],
)
def test_row_width_mismatch_is_structured_instead_of_dropping_or_filling_cells(
    data_row: str,
) -> None:
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "\n"
        f"{data_row}\n"
    ).encode()

    result = next(CSVAdapter(payload, dataset_code="quarterly_metric").iter_rows())

    assert result.row_number == 3
    assert result.value is None
    assert result.error is not None
    assert result.error.error_code == "ROW_COLUMN_COUNT_MISMATCH"

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.dgc_sap_dividend_detail import (
    DGC_SAP_DIVIDEND_DETAIL_FIELDS,
    DGC_SAP_DIVIDEND_DETAIL_SCOPED_FIELDS,
    DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME,
    DIVIDEND_GL_ACCOUNTS,
    INCOME_TAX_EXPENSE_GL_ACCOUNTS,
    OTHER_INCOME_GL_ACCOUNTS,
    TAXES_PAYABLE_GL_ACCOUNTS,
    DgcSapDividendDetailAdapter,
    DgcSapDividendDetailError,
    DgcSapDividendDetailRecord,
    DgcSapDividendDetailResult,
    DgcSapDividendMetricAdapter,
    DgcSettlementIncomeTaxExpenseAdapter,
    DgcSettlementIncomeTaxExpenseResult,
    DgcSettlementOtherIncomeAdapter,
    DgcSettlementTaxesPayableAdapter,
    DgcSettlementTaxesPayableResult,
)
from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


def test_dataset_uses_the_confirmed_business_table_name() -> None:
    assert DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME == "汇算清缴相关科目明细"


def test_adapter_filters_and_sums_exact_ksl_then_reverses_sign_once() -> None:
    result = _adapt(
        _row(
            voucher_no="100001",
            gl_account="6111010000",
            header_text="收到子公司分红",
            amount_ksl=Decimal("-10.123456789011"),
            debit_credit_flag="H",
        ),
        _row(
            voucher_no="100002",
            gl_account="6111150000",
            detail_text="本期确认股利",
            amount_ksl="-20.000000000001",
            debit_credit_flag="S",
        ),
        _row(
            voucher_no="100003",
            gl_account="6111990000",
            header_text="收到利润分配款",
            amount_ksl=-3,
        ),
        _row(
            voucher_no="not-an-account-match",
            gl_account="6001000000",
            header_text="分红",
            amount_ksl="-1000",
        ),
        _row(
            voucher_no="not-a-keyword-match",
            gl_account="6111020000",
            header_text="投资收益",
            detail_text="权益法核算",
            amount_ksl="-2000",
        ),
    )

    assert [record.voucher_no for record in result.records] == ["100001", "100002", "100003"]
    assert result.raw_ksl_total == Decimal("-33.123456789012")
    assert result.cumulative_dividend_amount == Decimal("33.123456789012")
    assert result.currency == "CNY"
    assert result.match_count == 3
    assert result.records[1].debit_credit_flag == "S"
    assert result.source_checksum == "a" * 64


def test_adapter_maps_all_published_response_fields_to_the_typed_record() -> None:
    result = _adapt(
        _row(
            companyname="Sentinel Company",
            fiscal_period="011",
            voucher_no="V-2026-001",
            header_text="收到分红",
            detail_text="sentinel detail",
            amount_ksl="-123.456789012345",
            gl_account="6111030000",
            account_name="投资收益-处置长期股权投资产生的投资收益",
            project_code="PROJECT-001",
            project_name="Sentinel Project",
            debit_credit_flag="H",
            group_currency="cny",
            original_system_doc_no="SOURCE-001",
        )
    )

    assert result.records == (
        DgcSapDividendDetailRecord(
            company="3730",
            companyname="Sentinel Company",
            fiscal_year=2026,
            fiscal_period=11,
            voucher_no="V-2026-001",
            header_text="收到分红",
            detail_text="sentinel detail",
            amount_ksl=Decimal("-123.456789012345"),
            gl_account="6111030000",
            account_name="投资收益-处置长期股权投资产生的投资收益",
            project_code="PROJECT-001",
            project_name="Sentinel Project",
            debit_credit_flag="H",
            group_currency="CNY",
            original_system_doc_no="SOURCE-001",
        ),
    )


def test_adapter_accepts_exact_scoped_schema_and_restores_requested_company() -> None:
    row = _row(header_text="收到分红", amount_ksl="-12.50")
    del row["company"]
    del row["companyname"]

    result = _adapt(row)

    assert set(row) == set(DGC_SAP_DIVIDEND_DETAIL_SCOPED_FIELDS)
    assert result.match_count == 1
    assert result.records[0].company == "3730"
    assert result.records[0].companyname is None


def test_adapter_rejects_extra_field_on_scoped_schema() -> None:
    row = _row()
    del row["company"]
    del row["companyname"]
    row["unexpected"] = "drift"

    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(row)

    assert raised.value.error_code == "UNEXPECTED_RESPONSE_FIELD"
    assert raised.value.field == "unexpected"


def test_adapter_does_not_create_a_currency_when_no_rows_match() -> None:
    result = _adapt(
        _row(gl_account="6111010000", header_text="投资收益", amount_ksl="1.2"),
    )

    assert result.records == ()
    assert result.raw_ksl_total == Decimal(0)
    assert result.cumulative_dividend_amount == Decimal(0)
    assert result.cumulative_dividend_amount.as_tuple().sign == 0
    assert result.currency is None
    assert result.match_count == 0


@pytest.mark.parametrize(
    ("source_period", "expected_period"),
    [
        ("1", 1),
        ("01", 1),
        ("001", 1),
        ("12", 12),
        ("012", 12),
    ],
)
def test_adapter_strictly_normalizes_supported_fiscal_period_formats(
    source_period: str,
    expected_period: int,
) -> None:
    result = _adapt(_row(fiscal_period=source_period, header_text="分红"))

    assert result.records[0].fiscal_period == expected_period


@pytest.mark.parametrize(
    "source_period",
    ["", "0", "000", "13", "013", "0001", "1.0", "１", 1, Decimal("1")],
)
def test_adapter_rejects_unsupported_fiscal_period_values(source_period: object) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(fiscal_period=source_period))

    assert raised.value.error_code == "INVALID_RESPONSE_VALUE"
    assert raised.value.field == "fiscal_period"


def test_adapter_sums_only_records_through_the_monitoring_period() -> None:
    result = _adapt(
        _row(voucher_no="q2", fiscal_period="006", header_text="分红", amount_ksl="-10"),
        _row(voucher_no="q3", fiscal_period="007", header_text="分红", amount_ksl="-20"),
        through_period=6,
    )

    assert [record.voucher_no for record in result.records] == ["q2"]
    assert result.raw_ksl_total == Decimal("-10")
    assert result.cumulative_dividend_amount == Decimal("10")


def test_adapter_validates_future_rows_before_excluding_them() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(
            _row(fiscal_period="006", header_text="分红", amount_ksl="-10"),
            _row(fiscal_period="007", header_text="分红", amount_ksl="NaN"),
            through_period=6,
        )

    assert raised.value.row_number == 2
    assert raised.value.field == "amount_ksl"


@pytest.mark.parametrize("gl_account", sorted(DIVIDEND_GL_ACCOUNTS))
def test_adapter_accepts_each_configured_dividend_account(gl_account: str) -> None:
    result = _adapt(_row(gl_account=gl_account, detail_text="收到股利", amount_ksl="-1"))

    assert result.match_count == 1
    assert result.records[0].gl_account == gl_account


def test_adapter_uses_enough_decimal_precision_for_the_exact_sum() -> None:
    result = _adapt(
        _row(
            voucher_no="large",
            header_text="分红",
            amount_ksl="-99999999999999999999999999.123456789011",
        ),
        _row(voucher_no="fraction", header_text="分红", amount_ksl="-0.000000000001"),
    )

    assert result.raw_ksl_total == Decimal("-99999999999999999999999999.123456789012")
    assert result.cumulative_dividend_amount == Decimal("99999999999999999999999999.123456789012")


def test_result_and_matching_records_are_immutable() -> None:
    result = _adapt(_row(header_text="分红", amount_ksl="-1"))

    with pytest.raises(FrozenInstanceError):
        result.match_count = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.records[0].voucher_no = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", DGC_SAP_DIVIDEND_DETAIL_FIELDS)
def test_adapter_rejects_every_missing_published_response_field(field: str) -> None:
    row = _row()
    del row[field]

    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(row)

    assert raised.value.error_code == "MISSING_RESPONSE_FIELD"
    assert raised.value.row_number == 1
    assert raised.value.field == field


def test_adapter_rejects_unexpected_response_fields() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(unpublished_field="contract drift"))

    assert raised.value.error_code == "UNEXPECTED_RESPONSE_FIELD"
    assert raised.value.field == "unpublished_field"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"company": "3000"}, "company"),
        ({"fiscal_year": "2025"}, "fiscal_year"),
    ],
)
def test_adapter_rejects_rows_outside_the_queried_company_year(
    override: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(**override))

    assert raised.value.error_code == "DGC_RESPONSE_SCOPE_MISMATCH"
    assert raised.value.field == field


def test_adapter_rejects_a_later_out_of_scope_row_instead_of_returning_partial_total() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(
            _row(voucher_no="valid", header_text="分红", amount_ksl="-10"),
            _row(voucher_no="wrong-year", fiscal_year="2025", header_text="分红"),
        )

    assert raised.value.row_number == 2
    assert raised.value.field == "fiscal_year"


@pytest.mark.parametrize("value", [1.25, float("inf"), float("nan"), True])
def test_adapter_rejects_float_and_boolean_ksl_values(value: object) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(amount_ksl=value))

    assert raised.value.error_code == "INVALID_RESPONSE_VALUE"
    assert raised.value.field == "amount_ksl"


@pytest.mark.parametrize(
    "value",
    [
        "1E+999999999",
        "1E-999999999",
        "100000000000000000000000000",
        "0.0000000000001",
    ],
)
def test_adapter_rejects_ksl_outside_the_database_numeric_envelope(value: str) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(amount_ksl=value))

    assert raised.value.error_code == "INVALID_RESPONSE_VALUE"
    assert raised.value.field == "amount_ksl"


def test_adapter_rejects_an_aggregate_outside_the_database_numeric_envelope() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(
            _row(voucher_no="first", header_text="分红", amount_ksl="9E+25"),
            _row(voucher_no="second", header_text="分红", amount_ksl="9E+25"),
        )

    assert raised.value.error_code == "AGGREGATE_AMOUNT_OUT_OF_RANGE"
    assert raised.value.field == "amount_ksl"


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "Infinity", ""])
def test_adapter_rejects_invalid_or_nonfinite_decimal_strings(value: str) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(amount_ksl=value))

    assert raised.value.field == "amount_ksl"


def test_adapter_rejects_mixed_currencies_only_when_both_rows_match() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(
            _row(voucher_no="cny", header_text="分红", group_currency="CNY"),
            _row(voucher_no="usd", detail_text="股利", group_currency="USD"),
        )

    assert raised.value.error_code == "MIXED_MATCHED_CURRENCIES"
    assert raised.value.field == "group_currency"

    result = _adapt(
        _row(voucher_no="cny", header_text="分红", group_currency="cny"),
        _row(
            voucher_no="unmatched-usd",
            header_text="ordinary investment income",
            group_currency="USD",
        ),
    )
    assert result.currency == "CNY"
    assert result.match_count == 1


def test_adapter_allows_present_but_blank_descriptive_fields() -> None:
    result = _adapt(
        _row(
            header_text="",
            detail_text="利润分配",
            project_code="",
            project_name="",
            original_system_doc_no="",
        )
    )

    assert result.match_count == 1
    assert result.records[0].project_code == ""


def test_adapter_normalizes_null_optional_fields_to_blank() -> None:
    result = _adapt(
        _row(
            header_text="分红",
            detail_text=None,
            project_code=None,
            project_name=None,
            original_system_doc_no=None,
        )
    )

    assert result.match_count == 1
    assert result.records[0].detail_text == ""
    assert result.records[0].project_code == ""
    assert result.records[0].project_name == ""
    assert result.records[0].original_system_doc_no == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("companyname", None),
        ("fiscal_period", 6),
        ("voucher_no", ""),
        ("gl_account", 6111010000),
        ("group_currency", ""),
    ],
)
def test_adapter_rejects_invalid_field_types_and_blank_identities(
    field: str,
    value: object,
) -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt(_row(**{field: value}))

    assert raised.value.error_code == "INVALID_RESPONSE_VALUE"
    assert raised.value.field == field


@pytest.mark.parametrize("expected_company", ["", "   ", 3730, None])
def test_adapter_rejects_invalid_expected_company(expected_company: object) -> None:
    with pytest.raises(ValueError, match="expected_company"):
        DgcSapDividendDetailAdapter(
            DgcFetchResult(records=(), checksum="a" * 64),
            expected_company=expected_company,  # type: ignore[arg-type]
            expected_fiscal_year=2026,
        )


@pytest.mark.parametrize("expected_year", [2026.0, True, "20X6", 1999, 10000])
def test_adapter_rejects_invalid_expected_year(expected_year: object) -> None:
    with pytest.raises(ValueError, match="expected_fiscal_year"):
        DgcSapDividendDetailAdapter(
            DgcFetchResult(records=(), checksum="a" * 64),
            expected_company="3730",
            expected_fiscal_year=expected_year,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("through_period", [0, 13, True, "6", 6.0])
def test_adapter_rejects_invalid_through_period(through_period: object) -> None:
    with pytest.raises(ValueError, match="through_period"):
        DgcSapDividendDetailAdapter(
            DgcFetchResult(records=(), checksum="a" * 64),
            expected_company="3730",
            expected_fiscal_year=2026,
            through_period=through_period,  # type: ignore[arg-type]
        )


def test_metric_adapter_materializes_exactly_one_received_dividends_row() -> None:
    result = _adapt(
        _row(fiscal_period="006", header_text="收到分红", amount_ksl="-12.50"),
        through_period=6,
    )
    adapter = DgcSapDividendMetricAdapter(
        result,
        company_code="3730",
        fiscal_year=2026,
        through_period=6,
        currency="cny",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
    )

    adapter.validate_header()
    rows = list(adapter.iter_rows())

    assert adapter.checksum == "a" * 64
    assert len(rows) == 1
    assert rows[0].error is None
    value = rows[0].value
    assert isinstance(value, CanonicalFinancialRow)
    assert value.company_code == "3730"
    assert value.fiscal_year == 2026
    assert value.period.isoformat() == "2026-06-30"
    assert value.currency == "CNY"
    assert value.amount_scale == 2
    assert value.metric_code == "received_dividends"
    assert value.amount == Decimal("12.50")
    assert value.source_record_key.startswith("dgc-sap-dividend-detail:")


def test_metric_adapter_uses_command_currency_for_evidenced_zero() -> None:
    result = _adapt(_row(header_text="ordinary investment income"), through_period=6)

    rows = list(
        DgcSapDividendMetricAdapter(
            result,
            company_code="3730",
            fiscal_year=2026,
            through_period=6,
            currency="CNY",
            amount_scale=2,
            extracted_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
        ).iter_rows()
    )

    value = rows[0].value
    assert isinstance(value, CanonicalFinancialRow)
    assert value.amount == Decimal(0)
    assert value.currency == "CNY"


def test_metric_adapter_rejects_matched_currency_mismatch() -> None:
    result = _adapt(_row(header_text="分红", group_currency="USD"))

    with pytest.raises(DgcSapDividendDetailError) as raised:
        DgcSapDividendMetricAdapter(
            result,
            company_code="3730",
            fiscal_year=2026,
            through_period=12,
            currency="CNY",
            amount_scale=2,
            extracted_at=datetime(2026, 12, 31, 8, tzinfo=timezone.utc),
        )

    assert raised.value.error_code == "DGC_RESPONSE_CURRENCY_MISMATCH"
    assert raised.value.field == "group_currency"


def test_other_income_adapter_filters_three_accounts_and_reverses_ksl_sign() -> None:
    fetched = DgcFetchResult(
        records=(
            _row(
                voucher_no="government-grant",
                gl_account="6112010000",
                amount_ksl="-10.25",
            ),
            _row(
                voucher_no="tax-refund",
                gl_account="6112020000",
                amount_ksl="-20.75",
            ),
            _row(
                voucher_no="tax-relief",
                gl_account="6112040000",
                amount_ksl="5",
            ),
            _row(
                voucher_no="not-other-income",
                gl_account="6111990000",
                amount_ksl="-999",
            ),
        ),
        checksum="b" * 64,
    )

    result = DgcSettlementOtherIncomeAdapter(
        fetched,
        expected_company="3730",
        expected_fiscal_year=2026,
    ).adapt()

    assert {record.gl_account for record in result.records} == OTHER_INCOME_GL_ACCOUNTS
    assert result.raw_ksl_total == Decimal("-26.00")
    assert result.other_income_amount == Decimal("26.00")
    assert result.match_count == 3
    assert result.currency == "CNY"
    assert result.source_checksum == "b" * 64


def test_other_income_adapter_applies_through_period_after_validating_all_rows() -> None:
    result = DgcSettlementOtherIncomeAdapter(
        DgcFetchResult(
            records=(
                _row(
                    voucher_no="q2",
                    fiscal_period="006",
                    gl_account="6112010000",
                    amount_ksl="-10",
                ),
                _row(
                    voucher_no="q3",
                    fiscal_period="007",
                    gl_account="6112020000",
                    amount_ksl="-20",
                ),
            ),
            checksum="c" * 64,
        ),
        expected_company="3730",
        expected_fiscal_year=2026,
        through_period=6,
    ).adapt()

    assert [record.voucher_no for record in result.records] == ["q2"]
    assert result.other_income_amount == Decimal("10")


def test_income_tax_expense_adapter_reverses_each_matching_line_without_aggregation() -> None:
    result = _adapt_income_tax_expense(
        _row(
            voucher_no="current-tax",
            gl_account="6801010000",
            amount_ksl="-10.123456789011",
        ),
        _row(
            voucher_no="deferred-tax",
            gl_account="6801020000",
            amount_ksl="-20.000000000001",
        ),
        _row(
            voucher_no="prior-year-tax",
            gl_account="6801030000",
            amount_ksl="5",
        ),
        _row(
            voucher_no="not-income-tax-expense",
            gl_account="6801990000",
            amount_ksl="-999",
        ),
        checksum="d" * 64,
    )

    assert {
        line.source_record.gl_account for line in result.lines
    } == INCOME_TAX_EXPENSE_GL_ACCOUNTS
    assert [line.source_record.voucher_no for line in result.lines] == [
        "current-tax",
        "deferred-tax",
        "prior-year-tax",
    ]
    assert [line.income_tax_expense_amount for line in result.lines] == [
        Decimal("10.123456789011"),
        Decimal("20.000000000001"),
        Decimal("-5"),
    ]
    assert result.match_count == 3
    assert result.source_checksum == "d" * 64


def test_income_tax_expense_adapter_applies_quarter_cutoff() -> None:
    result = _adapt_income_tax_expense(
        _row(
            voucher_no="q2",
            fiscal_period="006",
            gl_account="6801010000",
            amount_ksl="-10",
        ),
        _row(
            voucher_no="q3",
            fiscal_period="007",
            gl_account="6801020000",
            amount_ksl="-20",
        ),
        through_period=6,
    )

    assert [line.source_record.voucher_no for line in result.lines] == ["q2"]
    assert [line.income_tax_expense_amount for line in result.lines] == [Decimal("10")]


def test_income_tax_expense_adapter_validates_future_rows_before_filtering() -> None:
    with pytest.raises(DgcSapDividendDetailError) as raised:
        _adapt_income_tax_expense(
            _row(
                voucher_no="q2",
                fiscal_period="006",
                gl_account="6801010000",
                amount_ksl="-10",
            ),
            _row(
                voucher_no="invalid-q3",
                fiscal_period="007",
                gl_account="6801020000",
                amount_ksl="NaN",
            ),
            through_period=6,
        )

    assert raised.value.row_number == 2
    assert raised.value.field == "amount_ksl"


def test_income_tax_expense_adapter_returns_no_lines_when_no_account_matches() -> None:
    result = _adapt_income_tax_expense(
        _row(gl_account="6801990000", amount_ksl="-1"),
    )

    assert result.lines == ()
    assert result.match_count == 0


def test_income_tax_expense_adapter_preserves_each_line_currency() -> None:
    result = _adapt_income_tax_expense(
        _row(gl_account="6801010000", group_currency="CNY", amount_ksl="-1"),
        _row(gl_account="6801020000", group_currency="USD", amount_ksl="-2"),
    )

    assert [line.source_record.group_currency for line in result.lines] == ["CNY", "USD"]
    assert [line.income_tax_expense_amount for line in result.lines] == [
        Decimal("1"),
        Decimal("2"),
    ]


def test_income_tax_expense_adapter_ignores_unmatched_currency() -> None:
    result = _adapt_income_tax_expense(
        _row(gl_account="6801010000", group_currency="CNY", amount_ksl="-1"),
        _row(gl_account="6801990000", group_currency="USD", amount_ksl="-2"),
    )

    assert len(result.lines) == 1
    assert result.lines[0].source_record.group_currency == "CNY"
    assert result.lines[0].income_tax_expense_amount == Decimal("1")


def test_income_tax_expense_adapter_normalizes_each_zero_to_positive_zero() -> None:
    result = _adapt_income_tax_expense(
        _row(gl_account="6801010000", amount_ksl="-0.00"),
        _row(gl_account="6801020000", amount_ksl="0.00"),
    )

    amounts = [line.income_tax_expense_amount for line in result.lines]
    assert amounts == [Decimal(0), Decimal(0)]
    assert all(amount.as_tuple().sign == 0 for amount in amounts)


def test_taxes_payable_adapter_uses_only_2221130000_and_reverses_each_line() -> None:
    result = _adapt_taxes_payable(
        _row(
            voucher_no="income-tax-payable",
            gl_account="2221130000",
            account_name="应交税费-企业所得税",
            amount_ksl="-100.125",
        ),
        _row(
            voucher_no="same-prefix-but-excluded",
            gl_account="2221010000",
            account_name="应交税费-应交增值税",
            amount_ksl="5",
        ),
        _row(
            voucher_no="not-taxes-payable",
            gl_account="2241010000",
            account_name="其他应付款",
            amount_ksl="-999",
        ),
        checksum="e" * 64,
    )

    assert TAXES_PAYABLE_GL_ACCOUNTS == frozenset({"2221130000"})
    assert [line.source_record.voucher_no for line in result.lines] == ["income-tax-payable"]
    assert [line.taxes_payable_amount for line in result.lines] == [Decimal("100.125")]
    assert result.match_count == 1
    assert result.source_checksum == "e" * 64


def test_taxes_payable_adapter_applies_period_cutoff_and_normalizes_zero() -> None:
    result = _adapt_taxes_payable(
        _row(
            voucher_no="q2-zero",
            fiscal_period="006",
            gl_account="2221130000",
            amount_ksl="-0.00",
        ),
        _row(
            voucher_no="q3",
            fiscal_period="007",
            gl_account="2221130000",
            amount_ksl="-20",
        ),
        through_period=6,
    )

    assert [line.source_record.voucher_no for line in result.lines] == ["q2-zero"]
    assert result.lines[0].taxes_payable_amount == Decimal(0)
    assert result.lines[0].taxes_payable_amount.as_tuple().sign == 0


def _adapt_income_tax_expense(
    *records: dict[str, object],
    through_period: int = 12,
    checksum: str = "a" * 64,
) -> DgcSettlementIncomeTaxExpenseResult:
    return DgcSettlementIncomeTaxExpenseAdapter(
        DgcFetchResult(records=records, checksum=checksum),
        expected_company="3730",
        expected_fiscal_year=2026,
        through_period=through_period,
    ).adapt()


def _adapt_taxes_payable(
    *records: dict[str, object],
    through_period: int = 12,
    checksum: str = "a" * 64,
) -> DgcSettlementTaxesPayableResult:
    return DgcSettlementTaxesPayableAdapter(
        DgcFetchResult(records=records, checksum=checksum),
        expected_company="3730",
        expected_fiscal_year=2026,
        through_period=through_period,
    ).adapt()


def _adapt(
    *records: dict[str, object],
    through_period: int = 12,
) -> DgcSapDividendDetailResult:
    return DgcSapDividendDetailAdapter(
        DgcFetchResult(records=records, checksum="a" * 64),
        expected_company="3730",
        expected_fiscal_year="2026",
        through_period=through_period,
    ).adapt()


def _row(**overrides: object) -> dict[str, object]:
    return {
        "company": "3730",
        "companyname": "Company 3730",
        "fiscal_year": "2026",
        "fiscal_period": "06",
        "voucher_no": "100000",
        "header_text": "",
        "detail_text": "",
        "amount_ksl": "0",
        "gl_account": "6111010000",
        "account_name": "投资收益",
        "project_code": "",
        "project_name": "",
        "debit_credit_flag": "H",
        "group_currency": "CNY",
        "original_system_doc_no": "",
    } | overrides

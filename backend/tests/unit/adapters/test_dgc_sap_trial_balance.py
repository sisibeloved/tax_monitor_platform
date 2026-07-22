from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.adapters.ingest.dgc_sap_trial_balance import (
    CURRENT_INCOME_TAX_GL_ACCOUNT,
    CURRENT_QUARTER_CURRENT_TAX_METRIC,
    DGC_SAP_TRIAL_BALANCE_FIELDS,
    PRIOR_QUARTER_CURRENT_TAX_METRIC,
    DgcSapTrialBalanceAdapter,
    DgcSapTrialBalanceError,
    DgcSapTrialBalanceMetricAdapter,
    DgcSapTrialBalanceResult,
)


def test_adapter_aggregates_prior_and_current_quarters_using_source_signs() -> None:
    result = _adapt(
        _row(fiscal_period="01", total_debit_amount="100", total_credit_amount="-10"),
        _row(fiscal_period="03", total_debit_amount="50", total_credit_amount="-5"),
        _row(fiscal_period="04", total_debit_amount="400", total_credit_amount="-100"),
        _row(fiscal_period="05", total_debit_amount="250", total_credit_amount="-50"),
        _row(fiscal_period="06", total_debit_amount="300", total_credit_amount="0"),
        _row(fiscal_period="07", total_debit_amount="9999", total_credit_amount="9999"),
        through_period=6,
    )

    assert result.prior_quarter_current_tax == Decimal("135")
    assert result.current_quarter_current_tax == Decimal("800")
    assert result.source_row_count == 6
    assert result.rows_through_period == 5
    assert result.source_checksum == "a" * 64


def test_first_quarter_has_evidenced_zero_prior_quarter_amount() -> None:
    result = _adapt(
        _row(fiscal_period="001", total_debit_amount="10", total_credit_amount="-2"),
        _row(fiscal_period="003", total_debit_amount="5", total_credit_amount="0"),
        through_period=3,
    )

    assert result.prior_quarter_current_tax == Decimal(0)
    assert result.current_quarter_current_tax == Decimal("13")


def test_adapter_uses_exact_decimal_precision_for_debit_plus_credit() -> None:
    result = _adapt(
        _row(
            fiscal_period="03",
            total_debit_amount="99999999999999999999999998.123456789011",
            total_credit_amount="0.000000000001",
        ),
        through_period=3,
    )

    assert result.current_quarter_current_tax == Decimal(
        "99999999999999999999999998.123456789012"
    )


def test_adapter_validates_future_rows_before_excluding_them() -> None:
    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(
            _row(fiscal_period="06", total_debit_amount="10"),
            _row(fiscal_period="07", total_debit_amount="NaN"),
            through_period=6,
        )

    assert raised.value.row_number == 2
    assert raised.value.field == "total_debit_amount"


@pytest.mark.parametrize("field", DGC_SAP_TRIAL_BALANCE_FIELDS)
def test_adapter_rejects_every_missing_published_field(field: str) -> None:
    row = _row()
    del row[field]

    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(row)

    assert raised.value.error_code == "MISSING_RESPONSE_FIELD"
    assert raised.value.field == field


def test_adapter_rejects_unexpected_response_field() -> None:
    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(_row(unpublished="drift"))

    assert raised.value.error_code == "UNEXPECTED_RESPONSE_FIELD"
    assert raised.value.field == "unpublished"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"company_code": "3560"}, "company_code"),
        ({"fiscal_year": "2025"}, "fiscal_year"),
        ({"gl_account_code": "6801020000"}, "gl_account_code"),
    ],
)
def test_adapter_rejects_rows_outside_requested_scope(
    override: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(_row(**override))

    assert raised.value.error_code == "DGC_RESPONSE_SCOPE_MISMATCH"
    assert raised.value.field == field


@pytest.mark.parametrize("value", [1.5, float("inf"), float("nan"), True, "NaN", ""])
def test_adapter_rejects_inexact_or_invalid_amounts(value: object) -> None:
    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(_row(total_debit_amount=value))

    assert raised.value.error_code == "INVALID_RESPONSE_VALUE"
    assert raised.value.field == "total_debit_amount"


def test_empty_response_is_evidenced_as_company_not_accruing_current_tax() -> None:
    result = _adapt()

    assert result.prior_quarter_current_tax == Decimal(0)
    assert result.current_quarter_current_tax == Decimal(0)
    assert result.source_row_count == 0
    assert result.rows_through_period == 0
    assert result.source_checksum == "a" * 64


def test_adapter_rejects_response_with_only_periods_after_requested_quarter() -> None:
    with pytest.raises(DgcSapTrialBalanceError) as raised:
        _adapt(_row(fiscal_period="07"), through_period=6)

    assert raised.value.error_code == "NO_ROWS_THROUGH_PERIOD"


def test_metric_adapter_materializes_the_two_quarterly_metrics() -> None:
    result = DgcSapTrialBalanceResult(
        prior_quarter_current_tax=Decimal("900000"),
        current_quarter_current_tax=Decimal("700000"),
        source_row_count=2,
        rows_through_period=2,
        source_checksum="a" * 64,
    )
    adapter = DgcSapTrialBalanceMetricAdapter(
        result,
        company_code="3000",
        fiscal_year=2026,
        through_period=6,
        currency="cny",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
    )

    rows = list(adapter.iter_rows())

    assert adapter.checksum == "a" * 64
    assert [row.row_number for row in rows] == [1, 2]
    values = [row.value for row in rows]
    assert all(isinstance(value, CanonicalFinancialRow) for value in values)
    metrics = {
        value.metric_code: value
        for value in values
        if isinstance(value, CanonicalFinancialRow)
    }
    assert set(metrics) == {
        PRIOR_QUARTER_CURRENT_TAX_METRIC,
        CURRENT_QUARTER_CURRENT_TAX_METRIC,
    }
    assert metrics[PRIOR_QUARTER_CURRENT_TAX_METRIC].amount == Decimal("900000")
    assert metrics[CURRENT_QUARTER_CURRENT_TAX_METRIC].amount == Decimal("700000")
    assert all(value.period.isoformat() == "2026-06-30" for value in metrics.values())
    assert all(
        value.source_record_key.startswith("dgc-sap-trial-balance:")
        for value in metrics.values()
    )


def _adapt(
    *records: dict[str, object],
    through_period: int = 6,
) -> DgcSapTrialBalanceResult:
    return DgcSapTrialBalanceAdapter(
        DgcFetchResult(records=records, checksum="a" * 64),
        expected_company_code="3000",
        expected_fiscal_year="2026",
        through_period=through_period,
    ).adapt()


def _row(**overrides: object) -> dict[str, object]:
    return {
        "company_code": "3000",
        "company_name": "Company 3000",
        "fiscal_year": "2026",
        "fiscal_period": "06",
        "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
        "gl_account_name": "所得税费用-当期所得税费用",
        "bank_center_code": "",
        "bank_account_number": "",
        "cost_center_code": "",
        "cost_center_name": "",
        "profit_center_code": "",
        "profit_center_name": "",
        "internal_order_code": "",
        "internal_order_name": "",
        "business_area_code": "",
        "business_area_name": "",
        "customer_code": "",
        "customer_name": "",
        "vendor_code": "",
        "vendor_name": "",
        "asset_code": "",
        "asset_name": "",
        "rstgr": "",
        "rstgr_name": "",
        "input_tax_process_method": "",
        "sfkf": "",
        "total_debit_amount": "0",
        "total_credit_amount": "0",
    } | overrides

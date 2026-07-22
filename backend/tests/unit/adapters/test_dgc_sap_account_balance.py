from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_account_balance import (
    DEFERRED_TAX_ASSET_GL_ACCOUNT,
    DGC_SAP_ACCOUNT_BALANCE_FIELDS,
    DgcSapAccountBalanceAdapter,
    DgcSapAccountBalanceError,
    DgcSapAccountBalanceMetricAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


def _row(
    account_code: str,
    closing_balance: object,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "account_code": account_code,
        "account_name": f"Account {account_code}",
        "closing_balance": closing_balance,
        "company_code": "3000",
        "company_name": "Company 3000",
        "credit_amount": "0.00",
        "debit_amount": "0.00",
        "fiscal_period": "006",
        "fiscal_year": "2026",
        "input_tax_process_method": "",
        "net_amount": "0.00",
        "opening_balance": "0.00",
        "sfkf": None,
    }
    row.update(overrides)
    return row


def _adapt(*rows: dict[str, object]):
    return DgcSapAccountBalanceAdapter(
        DgcFetchResult(records=tuple(rows), checksum="a" * 64),
        expected_company_code="3000",
        expected_fiscal_year="2026",
        expected_fiscal_period=6,
    ).adapt()


def test_published_response_contract_is_exact() -> None:
    assert DGC_SAP_ACCOUNT_BALANCE_FIELDS == (
        "account_code",
        "account_name",
        "closing_balance",
        "company_code",
        "company_name",
        "credit_amount",
        "debit_amount",
        "fiscal_period",
        "fiscal_year",
        "input_tax_process_method",
        "net_amount",
        "opening_balance",
        "sfkf",
    )


def test_other_payables_only_converts_each_negative_target_balance_to_positive() -> None:
    result = _adapt(
        _row("2241050100", "100.00"),
        _row("2241050200", "-40.25"),
        _row("2241050900", Decimal("-9.75")),
        _row("2241059900", "0.00"),
        _row("2241060000", "-999.00"),
    )

    assert result.other_payables_accrual == Decimal("50.00")
    assert tuple(item.account_code for item in result.other_payables_records) == (
        "2241050100",
        "2241050200",
        "2241050900",
        "2241059900",
    )


def test_deferred_tax_uses_account_1811030000_closing_balance_without_sign_change() -> None:
    result = _adapt(
        _row(DEFERRED_TAX_ASSET_GL_ACCOUNT, "123.45"),
        _row(DEFERRED_TAX_ASSET_GL_ACCOUNT, "-23.45", sfkf="Y"),
    )

    assert result.sap_cumulative_deferred_tax_expense == Decimal("100.00")


def test_missing_deferred_tax_account_becomes_evidenced_positive_zero() -> None:
    result = _adapt(_row("1001000000", "1"))

    assert result.other_payables_accrual is None
    assert result.deferred_tax_records == ()
    assert result.sap_cumulative_deferred_tax_expense == Decimal(0)
    assert result.sap_cumulative_deferred_tax_expense.as_tuple().sign == 0


def test_empty_valid_response_also_evidences_no_deferred_tax_accrual() -> None:
    result = _adapt()

    assert result.records == ()
    assert result.sap_cumulative_deferred_tax_expense == Decimal(0)
    assert result.source_checksum == "a" * 64


def test_present_nonnegative_other_payables_are_evidenced_zero() -> None:
    result = _adapt(_row("2241050100", "1"), _row("2241050200", "0"))

    assert result.other_payables_accrual == Decimal(0)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"company_code": "3560"}, "company_code"),
        ({"fiscal_year": "2025"}, "fiscal_year"),
        ({"fiscal_period": "005"}, "fiscal_period"),
    ],
)
def test_response_scope_mismatch_is_rejected(
    overrides: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(DgcSapAccountBalanceError) as caught:
        _adapt(_row("2241050100", "-1", **overrides))

    assert caught.value.error_code == "DGC_RESPONSE_SCOPE_MISMATCH"
    assert caught.value.field == field


def test_schema_drift_is_rejected() -> None:
    row = _row("2241050100", "-1")
    row["unexpected"] = "value"

    with pytest.raises(DgcSapAccountBalanceError) as caught:
        _adapt(row)

    assert caught.value.error_code == "UNEXPECTED_RESPONSE_FIELD"


@pytest.mark.parametrize("value", [1.5, True, "NaN", "Infinity", ""])
def test_amounts_must_be_exact_finite_database_values(value: object) -> None:
    with pytest.raises(DgcSapAccountBalanceError) as caught:
        _adapt(_row("2241050100", value))

    assert caught.value.error_code == "INVALID_RESPONSE_VALUE"
    assert caught.value.field == "closing_balance"


def test_metric_adapter_emits_zero_deferred_tax_when_account_is_absent() -> None:
    result = _adapt(_row("2241050900", "-25"))
    adapter = DgcSapAccountBalanceMetricAdapter(
        result,
        company_code="3000",
        fiscal_year=2026,
        fiscal_period=6,
        currency="CNY",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    rows = tuple(adapter.iter_rows())
    assert all(row.error is None for row in rows)
    metrics = {
        row.value.metric_code: row.value.amount
        for row in rows
        if isinstance(row.value, CanonicalFinancialRow)
    }
    assert metrics == {
        "other_payables_accrual": Decimal("25"),
        "sap_cumulative_deferred_tax_expense": Decimal(0),
    }
    assert all(
        isinstance(row.value, CanonicalFinancialRow)
        and row.value.period.isoformat() == "2026-06-30"
        for row in rows
    )


def test_metric_adapter_emits_both_quarterly_metrics_when_present() -> None:
    result = _adapt(
        _row("2241050900", "-25"),
        _row(DEFERRED_TAX_ASSET_GL_ACCOUNT, "80"),
    )
    adapter = DgcSapAccountBalanceMetricAdapter(
        result,
        company_code="3000",
        fiscal_year=2026,
        fiscal_period=6,
        currency="CNY",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    metrics = {
        row.value.metric_code: row.value.amount
        for row in adapter.iter_rows()
        if isinstance(row.value, CanonicalFinancialRow)
    }
    assert metrics == {
        "other_payables_accrual": Decimal("25"),
        "sap_cumulative_deferred_tax_expense": Decimal("80"),
    }

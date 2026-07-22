from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_account_balance import (
    DgcSapAccountBalanceAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_dividend_detail import DgcSapDividendDetailAdapter
from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcFetchResult,
    DgcSapProfitAdapter,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
)
from tax_risk.adapters.ingest.dgc_sap_trial_balance import (
    CURRENT_INCOME_TAX_GL_ACCOUNT,
    DgcSapTrialBalanceAdapter,
)
from tax_risk.adapters.lark.legal_entity_metrics import (
    LarkLegalEntityMetricFieldMap,
    LarkLegalEntityMetricsAdapter,
)
from tax_risk.domain.money import Money
from tax_risk.domain.quarterly import CalculationStatus, QuarterlyInputs, calculate_quarterly


def test_quarterly_accrual_check_uses_the_completed_variable_source_rules() -> None:
    profit_metrics = _profit_metrics()
    field_map = LarkLegalEntityMetricFieldMap()
    master = LarkLegalEntityMetricsAdapter(
        (
            {
                field_map.company_code: "3000",
                field_map.company_name: "Company 3000",
                field_map.tax_rate: 0.25,
                field_map.deferred_tax_rate: 0.25,
                field_map.loss_carryforward: 2_000_000,
                field_map.three_year_average_tax_burden: 0.09,
            },
        ),
        valid_from=date(2026, 1, 1),
    ).parse().rows[0]
    dividend = DgcSapDividendDetailAdapter(
        DgcFetchResult(
            records=(
                {
                    "company": "3000",
                    "companyname": "Company 3000",
                    "fiscal_year": "2026",
                    "fiscal_period": "06",
                    "voucher_no": "DIV-001",
                    "header_text": "",
                    "detail_text": "收到子公司股利",
                    "amount_ksl": "-1000000",
                    "gl_account": "6111020000",
                    "account_name": "投资收益-成本法核算的长期股权投资收益",
                    "project_code": "",
                    "project_name": "",
                    "debit_credit_flag": "H",
                    "group_currency": "CNY",
                    "original_system_doc_no": "SOURCE-DIV-001",
                },
            ),
            checksum="b" * 64,
        ),
        expected_company="3000",
        expected_fiscal_year="2026",
        through_period=6,
    ).adapt()
    trial_balance = DgcSapTrialBalanceAdapter(
        DgcFetchResult(
            records=(
                _trial_row(fiscal_period="03", total_debit_amount="900000"),
                _trial_row(fiscal_period="06", total_debit_amount="700000"),
            ),
            checksum="c" * 64,
        ),
        expected_company_code="3000",
        expected_fiscal_year="2026",
        through_period=6,
    ).adapt()
    account_balance = DgcSapAccountBalanceAdapter(
        DgcFetchResult(
            records=(_account_balance_row("2241050900", "-1400000"),),
            checksum="d" * 64,
        ),
        expected_company_code="3000",
        expected_fiscal_year="2026",
        expected_fiscal_period=6,
    ).adapt()
    assert account_balance.other_payables_accrual is not None

    result = calculate_quarterly(
        QuarterlyInputs(
            cumulative_profit=_money(profit_metrics["cumulative_profit"]),
            received_dividends=_money(dividend.cumulative_dividend_amount),
            fair_value_change=_money(profit_metrics["fair_value_change"]),
            loss_carryforward=_money(master.loss_carryforward),
            tax_rate=master.tax_rate,
            prior_quarter_current_tax=_money(
                trial_balance.prior_quarter_current_tax
            ),
            current_quarter_current_tax=_money(
                trial_balance.current_quarter_current_tax
            ),
            cumulative_revenue=_money(profit_metrics["cumulative_revenue"]),
            historical_average_tax_burden=master.three_year_average_tax_burden,
            other_payables_accrual=_money(account_balance.other_payables_accrual),
            hesi_no_invoice=_money("0"),
        )
    )

    assert profit_metrics == {
        "cumulative_profit": Decimal("10000000"),
        "fair_value_change": Decimal("500000"),
        "cumulative_revenue": Decimal("50000000"),
    }
    assert dividend.cumulative_dividend_amount == Decimal("1000000")
    assert trial_balance.prior_quarter_current_tax == Decimal("900000")
    assert trial_balance.current_quarter_current_tax == Decimal("700000")
    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.base_before_floor is not None
    assert result.base_before_floor.amount == Decimal("6500000")
    assert result.cumulative_tax_payable is not None
    assert result.cumulative_tax_payable.amount == Decimal("1625000.00")
    assert result.current_quarter_should_accrue is not None
    assert result.current_quarter_should_accrue.amount == Decimal("725000.00")
    assert result.current_quarter_difference is not None
    assert result.current_quarter_difference.amount == Decimal("25000.00")
    assert result.accrual_alert_flag is True
    assert result.accrual_alert_code == "UNDER_ACCRUED"


def _profit_metrics() -> dict[str, Decimal]:
    adapter = DgcSapProfitAdapter(
        DgcFetchResult(
            records=(
                _profit_row(
                    '四、利润总额(损失以"-"号填列)',
                    "10000000",
                    line_number="40",
                ),
                _profit_row(
                    '公允价值变动收益(损失以"-"号填列)',
                    "500000",
                    line_number="20",
                ),
                _profit_row("一、营业总收入", "50000000", line_number="10"),
            ),
            checksum="a" * 64,
        ),
        field_map=DgcSapProfitFieldMap(),
        metric_map=DgcSapProfitMetricMap(),
        ledger="0L",
        expected_company_code="3000",
        currency="CNY",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
    )
    metrics: dict[str, Decimal] = {}
    for parsed in adapter.iter_rows():
        assert parsed.error is None
        assert isinstance(parsed.value, CanonicalFinancialRow)
        metrics[parsed.value.metric_code] = parsed.value.amount
    return metrics


def _profit_row(line_item: str, amount: str, *, line_number: str) -> dict[str, object]:
    return {
        "mandt": "100",
        "bukrs": "3000",
        "companyname": "Company 3000",
        "gjahr": "2026",
        "monat": "06",
        "rldnr": "0L",
        "hs": line_number,
        "ztext": line_item,
        "nmhsl": "0",
        "nyhsl": amount,
    }


def _trial_row(**overrides: object) -> dict[str, object]:
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


def _account_balance_row(account_code: str, closing_balance: str) -> dict[str, object]:
    return {
        "account_code": account_code,
        "account_name": "Other payables accrual",
        "closing_balance": closing_balance,
        "company_code": "3000",
        "company_name": "Company 3000",
        "credit_amount": "0",
        "debit_amount": "0",
        "fiscal_period": "006",
        "fiscal_year": "2026",
        "input_tax_process_method": "",
        "net_amount": "0",
        "opening_balance": "0",
        "sfkf": "",
    }


def _money(value: Decimal | str) -> Money:
    return Money.unrounded(value, currency="CNY", scale=2)

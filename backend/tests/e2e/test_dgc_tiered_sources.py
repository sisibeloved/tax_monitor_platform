from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_account_balance import (
    DgcSapAccountBalanceAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_dividend_detail import (
    DgcSapDividendDetailAdapter,
    DgcSettlementIncomeTaxExpenseAdapter,
    DgcSettlementOtherIncomeAdapter,
)
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
from tax_risk.config import Settings
from tax_risk.application.dgc_hesi_invoice import (
    DgcHesiInvoiceQuery,
    DgcHesiInvoiceQueryService,
)
from tax_risk.application.dgc_hesi_reimbursement import (
    DgcHesiReimbursementQuery,
    DgcHesiReimbursementQueryService,
)
from tests.support.tiered_dgc import (
    DgcInterface,
    SourceMode,
    TieredDgcSource,
    build_tiered_source,
    data_status,
    load_tiered_settings,
    source_report,
    tiered_config,
)


COMPANY = "3000"
FISCAL_YEAR = "2026"
THROUGH_PERIOD = 6


@pytest.fixture(scope="module")
def tiered_settings() -> Settings:
    return load_tiered_settings()


@pytest.mark.tiered_interface
def test_profit_statement_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=(
            _profit_row("四、利润总额", "39661962.74", "1"),
            _profit_row("公允价值变动收益", "0", "2"),
            _profit_row("一、营业总收入", "100000000", "3"),
        ),
        checksum="1" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.SAP_PROFIT),
        mock,
    ) as source:
        fetched = source.fetch(
            {"bukrs": COMPANY, "gjahr": FISCAL_YEAR, "monat": "06"}
        )
        _record_source(request, source, fetched)
        if not fetched.records:
            return
        adapter = DgcSapProfitAdapter(
            fetched,
            field_map=DgcSapProfitFieldMap(**tiered_settings.dgc_sap_profit_field_map),
            metric_map=DgcSapProfitMetricMap(**tiered_settings.dgc_sap_profit_metric_map),
            ledger=tiered_settings.dgc_sap_profit_ledger,
            expected_company_code=COMPANY,
            currency="CNY",
            amount_scale=2,
            extracted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        rows = tuple(adapter.iter_rows())
        assert not [row.error for row in rows if row.error is not None]
        if source.mode is SourceMode.MOCK:
            amounts = {
                row.value.metric_code: row.value.amount
                for row in rows
                if isinstance(row.value, CanonicalFinancialRow)
            }
            assert amounts == {
                "cumulative_profit": Decimal("39661962.74"),
                "fair_value_change": Decimal("0"),
                "cumulative_revenue": Decimal("100000000"),
            }


@pytest.mark.tiered_interface
def test_trial_balance_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=(
            _trial_balance_row("03", "100", "-10"),
            _trial_balance_row("06", "200", "-20"),
        ),
        checksum="2" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.SAP_TRIAL_BALANCE),
        mock,
    ) as source:
        fetched = source.fetch(
            {
                "company_code": COMPANY,
                "fiscal_year": FISCAL_YEAR,
                "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
            }
        )
        _record_source(request, source, fetched)
        result = DgcSapTrialBalanceAdapter(
            fetched,
            expected_company_code=COMPANY,
            expected_fiscal_year=FISCAL_YEAR,
            through_period=THROUGH_PERIOD,
        ).adapt()
        assert result.source_row_count == len(fetched.records)
        if not fetched.records:
            assert result.prior_quarter_current_tax == Decimal(0)
            assert result.current_quarter_current_tax == Decimal(0)
        if source.mode is SourceMode.MOCK:
            assert result.prior_quarter_current_tax == Decimal("90")
            assert result.current_quarter_current_tax == Decimal("180")


@pytest.mark.tiered_interface
def test_dividend_detail_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=(
            _dividend_row(),
            _dividend_row(
                voucher_no="V-2026-002",
                gl_account="6112010000",
                amount_ksl="-12.34",
            ),
            _dividend_row(
                voucher_no="V-2026-003",
                gl_account="6801010000",
                amount_ksl="-56.78",
            ),
        ),
        checksum="3" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.SAP_DIVIDEND_DETAIL),
        mock,
    ) as source:
        fetched = source.fetch({"company": COMPANY, "fiscal_year": FISCAL_YEAR})
        _record_source(request, source, fetched)
        if not fetched.records:
            return
        result = DgcSapDividendDetailAdapter(
            fetched,
            expected_company=COMPANY,
            expected_fiscal_year=FISCAL_YEAR,
            through_period=THROUGH_PERIOD,
        ).adapt()
        other_income = DgcSettlementOtherIncomeAdapter(
            fetched,
            expected_company=COMPANY,
            expected_fiscal_year=FISCAL_YEAR,
            through_period=THROUGH_PERIOD,
        ).adapt()
        income_tax_expense = DgcSettlementIncomeTaxExpenseAdapter(
            fetched,
            expected_company=COMPANY,
            expected_fiscal_year=FISCAL_YEAR,
            through_period=THROUGH_PERIOD,
        ).adapt()
        assert result.match_count <= len(fetched.records)
        assert other_income.match_count <= len(fetched.records)
        assert income_tax_expense.match_count <= len(fetched.records)
        if source.mode is SourceMode.MOCK:
            assert result.cumulative_dividend_amount == Decimal("123.45")
            assert other_income.other_income_amount == Decimal("12.34")
            assert [
                line.income_tax_expense_amount for line in income_tax_expense.lines
            ] == [Decimal("56.78")]


@pytest.mark.tiered_interface
def test_account_balance_transport_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=(
            {
                "account_code": "2241050900",
                "account_name": "其他应付款-暂估/预提款-应付暂估款",
                "closing_balance": "-100",
                "company_code": COMPANY,
                "company_name": "Company 3000",
                "credit_amount": "0",
                "debit_amount": "0",
                "fiscal_year": FISCAL_YEAR,
                "fiscal_period": "006",
                "input_tax_process_method": "",
                "net_amount": "0",
                "opening_balance": "0",
                "sfkf": "",
            },
        ),
        checksum="4" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.SAP_ACCOUNT_BALANCE),
        mock,
    ) as source:
        fetched = source.fetch(
            {
                "company_code": COMPANY,
                "fiscal_year": FISCAL_YEAR,
                "fiscal_period": "006",
            }
        )
        _record_source(request, source, fetched)
        result = DgcSapAccountBalanceAdapter(
            fetched,
            expected_company_code=COMPANY,
            expected_fiscal_year=FISCAL_YEAR,
            expected_fiscal_period=THROUGH_PERIOD,
        ).adapt()
        if source.mode is SourceMode.MOCK:
            assert result.other_payables_accrual == Decimal("100")


@pytest.mark.tiered_interface
def test_hesi_reimbursement_transport_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=(
            {
                "company_code": COMPANY,
                "expense_code": "C-MOCK-001",
                "flow_end_date": "2026-06-30",
                "fee_type_code": "F1000",
                "fee_type_amount": Decimal("100"),
            },
        ),
        checksum="5" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.HESI_REIMBURSEMENT),
        mock,
    ) as source:
        fetched = DgcHesiReimbursementQueryService(source).query(
            DgcHesiReimbursementQuery(company_code=COMPANY)
        )
        _record_source(request, source, fetched)

        if source.mode is SourceMode.MOCK:
            assert fetched.records == mock.records


@pytest.mark.tiered_interface
def test_hesi_invoice_transport_uses_real_or_deterministic_mock(
    request: pytest.FixtureRequest,
    tiered_settings: Settings,
) -> None:
    mock = DgcFetchResult(
        records=({"company_code": COMPANY, "invoice_id": "INV-MOCK-001"},),
        checksum="5" * 64,
    )
    with build_tiered_source(
        tiered_config(tiered_settings, DgcInterface.HESI_INVOICE),
        mock,
    ) as source:
        fetched = DgcHesiInvoiceQueryService(source).query(
            DgcHesiInvoiceQuery(company_code=COMPANY)
        )
        _record_source(request, source, fetched)

        if source.mode is SourceMode.MOCK:
            assert fetched.records == (
                {"company_code": COMPANY, "invoice_id": "INV-MOCK-001"},
            )


def _record_source(
    request: pytest.FixtureRequest,
    source: TieredDgcSource,
    result: DgcFetchResult,
) -> None:
    status = data_status(result)
    request.node.user_properties.extend(
        (
            ("interface_source", source.interface.value),
            ("source_mode", source.mode.value),
            ("data_status", status.value),
            ("record_count", len(result.records)),
        )
    )
    print(source_report(source, result), flush=True)


def _profit_row(label: str, ytd_amount: str, line_number: str) -> dict[str, object]:
    return {
        "mandt": "800",
        "bukrs": COMPANY,
        "companyname": "Company 3000",
        "gjahr": FISCAL_YEAR,
        "monat": "06",
        "rldnr": "0L",
        "hs": line_number,
        "ztext": label,
        "nmhsl": "0",
        "nyhsl": ytd_amount,
    }


def _trial_balance_row(
    period: str,
    debit: str,
    credit: str,
) -> dict[str, object]:
    return {
        "company_code": COMPANY,
        "company_name": "Company 3000",
        "fiscal_year": FISCAL_YEAR,
        "fiscal_period": period,
        "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
        "gl_account_name": "Current income tax expense",
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
        "total_debit_amount": debit,
        "total_credit_amount": credit,
    }


def _dividend_row(**overrides: object) -> dict[str, object]:
    return {
        "company": COMPANY,
        "companyname": "Company 3000",
        "fiscal_year": FISCAL_YEAR,
        "fiscal_period": "06",
        "voucher_no": "V-2026-001",
        "header_text": "收到子公司分红",
        "detail_text": "",
        "amount_ksl": "-123.45",
        "gl_account": "6111010000",
        "account_name": "投资收益",
        "project_code": "",
        "project_name": "",
        "debit_credit_flag": "H",
        "group_currency": "CNY",
        "original_system_doc_no": "",
    } | overrides

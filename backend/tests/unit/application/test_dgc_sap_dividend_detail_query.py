from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.dgc_sap_dividend_detail import (
    DgcDividendParameterValue,
    DgcSapDividendDetailQuery,
    DgcSapDividendDetailQueryService,
)


class _Source:
    def __init__(self, result: DgcFetchResult) -> None:
        self.result = result
        self.parameters: Mapping[str, DgcDividendParameterValue] | None = None

    def fetch(
        self,
        parameters: Mapping[str, DgcDividendParameterValue],
    ) -> DgcFetchResult:
        self.parameters = parameters
        return self.result


def test_query_sends_only_company_and_year_then_returns_exact_calculation() -> None:
    source = _Source(
        DgcFetchResult(
            records=(
                _row(
                    header_text="收到子公司分红",
                    amount_ksl=Decimal("-123.456789012345"),
                ),
            ),
            checksum="a" * 64,
        )
    )
    service = DgcSapDividendDetailQueryService(source)

    result = service.query(DgcSapDividendDetailQuery(company=" 3730 ", fiscal_year=" 2026 "))

    assert source.parameters == {"company": "3730", "fiscal_year": "2026"}
    assert "fiscal_period" not in source.parameters
    assert "limitValue" not in source.parameters
    assert "offsetValue" not in source.parameters
    assert result.raw_ksl_total == Decimal("-123.456789012345")
    assert result.cumulative_dividend_amount == Decimal("123.456789012345")


def test_query_applies_local_period_cutoff_without_narrowing_remote_request() -> None:
    source = _Source(
        DgcFetchResult(
            records=(
                _row(fiscal_period="006", header_text="分红", amount_ksl="-10"),
                _row(fiscal_period="007", header_text="分红", amount_ksl="-20"),
            ),
            checksum="a" * 64,
        )
    )

    result = DgcSapDividendDetailQueryService(source).query(
        DgcSapDividendDetailQuery(
            company="3730",
            fiscal_year="2026",
            through_period=6,
        )
    )

    assert source.parameters == {"company": "3730", "fiscal_year": "2026"}
    assert result.cumulative_dividend_amount == Decimal("10")
    assert [record.fiscal_period for record in result.records] == [6]


@pytest.mark.parametrize(
    "query",
    [
        DgcSapDividendDetailQuery(company="", fiscal_year="2026"),
        DgcSapDividendDetailQuery(company="   ", fiscal_year="2026"),
        DgcSapDividendDetailQuery(company="3730", fiscal_year="26"),
        DgcSapDividendDetailQuery(company="3730", fiscal_year="２０２６"),
        DgcSapDividendDetailQuery(company="3730", fiscal_year="1999"),
        DgcSapDividendDetailQuery(company="3730", fiscal_year="2026", through_period=0),
        DgcSapDividendDetailQuery(company="3730", fiscal_year="2026", through_period=13),
        DgcSapDividendDetailQuery(
            company="3730",
            fiscal_year="2026",
            through_period=True,
        ),
    ],
)
def test_query_rejects_invalid_scope_before_calling_source(
    query: DgcSapDividendDetailQuery,
) -> None:
    source = _Source(DgcFetchResult(records=(), checksum="a" * 64))
    service = DgcSapDividendDetailQueryService(source)

    with pytest.raises(ValueError):
        service.query(query)

    assert source.parameters is None


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

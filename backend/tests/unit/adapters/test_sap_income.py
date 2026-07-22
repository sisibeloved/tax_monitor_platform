from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest

from tax_risk.adapters.dgc.sap_income import (
    SAP_INCOME_FIELDS,
    SapIncomeClient,
    SapIncomeClientConfiguration,
    SapIncomeClientError,
)


ENDPOINT = "https://116.63.221.181/post/sapincome"


def _raw_row(period: str, *, hs: str = "28") -> dict[str, object]:
    return {
        "mandt": "800",
        "bukrs": "3HD0",
        "companyname": "测试公司",
        "gjahr": "2025",
        "monat": period,
        "rldnr": "0L",
        "hs": hs,
        "ztext": "四、利润总额（损失以“－”号填列）",
        "nmhsl": "100.00",
        "nyhsl": "680731.35",
    }


def _configuration(**overrides: object) -> SapIncomeClientConfiguration:
    values: dict[str, object] = {
        "endpoint": ENDPOINT,
        "app_key": "test-key",
        "app_secret": "test-secret",
        "page_size": 2,
    }
    values.update(overrides)
    return SapIncomeClientConfiguration.model_validate(values)


def test_client_normalizes_month_and_paginates_using_total_size() -> None:
    offsets: list[int] = []
    month_inputs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["offsetValue"])
        month_inputs.append(body["monat"])
        rows = (
            [_raw_row("03"), _raw_row("03", hs="29")]
            if body["offsetValue"] == 0
            else [_raw_row("03", hs="30")]
        )
        return httpx.Response(
            200,
            headers={"x-request-id": "income-1"},
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "totalSize": "3",
                    "rowSize": len(rows),
                    "columnNames": list(SAP_INCOME_FIELDS),
                    "data": rows,
                },
            },
        )

    with SapIncomeClient(
        _configuration(),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2025, 7, 17, tzinfo=timezone.utc),
    ) as client:
        rows = client.fetch_rows(
            company_code="3HD0",
            fiscal_year="2025",
            fiscal_period="003",
        )

    assert offsets == [0, 2]
    assert month_inputs == ["03", "03"]
    assert len(rows) == 3
    assert rows[0].nyhsl == Decimal("680731.35")


def test_client_rejects_period_scope_mismatch() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        row = _raw_row("02")
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "totalSize": "1",
                    "rowSize": 1,
                    "columnNames": list(SAP_INCOME_FIELDS),
                    "data": [row],
                },
            },
        )

    with SapIncomeClient(
        _configuration(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(SapIncomeClientError) as captured:
            client.fetch_rows(
                company_code="3HD0",
                fiscal_year="2025",
                fiscal_period="03",
            )

    assert captured.value.error_code == "DGC_PERIOD_SCOPE_MISMATCH"

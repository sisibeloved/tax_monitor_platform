from datetime import datetime, timezone
import json

import httpx
import pytest

from tax_risk.adapters.dgc.trial_balance import (
    TRIAL_BALANCE_FIELDS,
    TrialBalanceClient,
    TrialBalanceClientConfiguration,
    TrialBalanceClientError,
)


ENDPOINT = "https://116.63.221.181/fin/trial_balance"


def _raw_row(period: str, account: str = "6600010000") -> dict[str, object]:
    values: dict[str, object] = {field: "" for field in TRIAL_BALANCE_FIELDS}
    values.update(
        {
            "company_code": "3HD0",
            "company_name": "测试公司",
            "fiscal_year": "2025",
            "fiscal_period": period,
            "gl_account_code": account,
            "gl_account_name": "工资薪金",
            "total_debit_amount": "100.25",
            "total_credit_amount": "-10.00",
        }
    )
    return values


def _configuration(**overrides: object) -> TrialBalanceClientConfiguration:
    values: dict[str, object] = {
        "endpoint": ENDPOINT,
        "app_key": "test-key",
        "app_secret": "test-secret",
        "page_size": 2,
    }
    values.update(overrides)
    return TrialBalanceClientConfiguration.model_validate(values)


def test_client_paginates_without_total_size() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["offsetValue"])
        assert body["company_code"] == "3HD0"
        assert body["fiscal_period"] == "001"
        rows = (
            [_raw_row("001"), _raw_row("001", "6600010001")]
            if body["offsetValue"] == 0
            else [_raw_row("001", "6600010002")]
        )
        return httpx.Response(
            200,
            headers={"x-request-id": "trial-1"},
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "totalSize": None,
                    "rowSize": len(rows),
                    "columnNames": list(TRIAL_BALANCE_FIELDS),
                    "data": rows,
                },
            },
        )

    with TrialBalanceClient(
        _configuration(),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2025, 7, 17, tzinfo=timezone.utc),
    ) as client:
        rows = client.fetch_rows(
            company_code="3HD0",
            fiscal_year="2025",
            fiscal_period="001",
        )

    assert offsets == [0, 2]
    assert len(rows) == 3
    assert rows[0].total_debit_amount + rows[0].total_credit_amount == pytest.approx(90.25)


def test_client_rejects_period_scope_mismatch() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        row = _raw_row("002")
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "rowSize": 1,
                    "columnNames": list(TRIAL_BALANCE_FIELDS),
                    "data": [row],
                },
            },
        )

    with TrialBalanceClient(
        _configuration(page_size=2),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(TrialBalanceClientError) as captured:
            client.fetch_rows(
                company_code="3HD0",
                fiscal_year="2025",
                fiscal_period="001",
            )

    assert captured.value.error_code == "DGC_PERIOD_SCOPE_MISMATCH"

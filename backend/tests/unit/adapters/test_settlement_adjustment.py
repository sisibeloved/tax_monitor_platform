from datetime import datetime, timezone
import json

import httpx
import pytest

from tax_risk.adapters.dgc.settlement_adjustment import (
    SCOPED_FIELDS,
    SettlementAdjustmentClient,
    SettlementAdjustmentClientConfiguration,
    SettlementAdjustmentClientError,
    build_apig_headers,
)


ENDPOINT = "https://116.63.221.181/post/settlement_adjustment"


def _raw_row(voucher: str, *, amount: str = "10.25") -> dict[str, object]:
    return {
        "fiscal_year": "2025",
        "fiscal_period": "001",
        "voucher_no": voucher,
        "header_text": "",
        "detail_text": "公益捐赠",
        "amount_ksl": amount,
        "gl_account": "6711060000",
        "account_name": "公益性捐赠",
        "project_code": "",
        "project_name": "",
        "debit_credit_flag": "S",
        "group_currency": "CNY",
        "original_system_doc_no": f"source-{voucher}",
    }


def _configuration(**overrides: object) -> SettlementAdjustmentClientConfiguration:
    values: dict[str, object] = {
        "endpoint": ENDPOINT,
        "app_key": "test-key",
        "app_secret": "test-secret",
        "page_size": 2,
    }
    values.update(overrides)
    return SettlementAdjustmentClientConfiguration.model_validate(values)


def test_signature_uses_validated_canonical_uri_trailing_slash() -> None:
    body = b'{"company":"3320","fiscal_year":"2025","offsetValue":0,"limitValue":15000}'

    headers = build_apig_headers(
        endpoint=ENDPOINT,
        app_key="test-key",
        app_secret="test-secret",
        body=body,
        sdk_date="20250717T000000Z",
    )

    assert headers["Authorization"] == (
        "SDK-HMAC-SHA256 Access=test-key, "
        "SignedHeaders=content-type;host;x-sdk-date, "
        "Signature=e4ceb0e1e7d8ef0fe4375074387b16db25ac02e1876f7649b29246d6c76bdb5f"
    )
    assert headers["x-Authorization"] == headers["Authorization"]


def test_client_paginates_scoped_thirteen_field_response() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["offsetValue"])
        assert request.url.path == "/post/settlement_adjustment"
        assert request.headers["authorization"] == request.headers["x-authorization"]
        rows = [_raw_row("1"), _raw_row("2")] if body["offsetValue"] == 0 else [_raw_row("3")]
        return httpx.Response(
            200,
            headers={"x-request-id": "request-1"},
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "totalSize": 3,
                    "rowSize": len(rows),
                    "columnNames": list(SCOPED_FIELDS),
                    "data": rows,
                },
            },
        )

    with SettlementAdjustmentClient(
        _configuration(),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2025, 7, 17, tzinfo=timezone.utc),
    ) as client:
        rows = client.fetch_rows(company="3320", fiscal_year="2025")

    assert offsets == [0, 2]
    assert [row.voucher_no for row in rows] == ["1", "2", "3"]
    assert all(row.company == "3320" for row in rows)


def test_client_rejects_schema_drift_without_leaking_secret() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "success": True,
                    "totalSize": 1,
                    "rowSize": 1,
                    "columnNames": [*SCOPED_FIELDS, "unexpected"],
                    "data": [{**_raw_row("1"), "unexpected": "value"}],
                },
            },
        )

    with SettlementAdjustmentClient(
        _configuration(app_secret="must-not-leak"),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(SettlementAdjustmentClientError) as captured:
            client.fetch_rows(company="3320", fiscal_year="2025")

    assert captured.value.error_code == "DGC_SCHEMA_DRIFT"
    assert "must-not-leak" not in str(captured.value)

from datetime import datetime, timezone
import json

import httpx
import pytest
from pydantic import SecretStr

from tax_risk.adapters.dgc.hesi_business_entertainment import (
    HESI_APPLICATION_FIELDS,
    HESI_DETAIL_CODE_FIELDS,
    HESI_DETAIL_FIELDS,
    HESI_INVOICE_FIELDS,
    HesiApplicationClient,
    HesiApplicationClientConfiguration,
    HesiBusinessDataClientError,
    HesiDetailClient,
    HesiDetailClientConfiguration,
    HesiInvoiceClient,
    HesiInvoiceClientConfiguration,
)


NOW = datetime(2025, 7, 17, tzinfo=timezone.utc)


def _raw_row(fields: tuple[str, ...], **values: object) -> dict[str, object]:
    row: dict[str, object] = {field: "" for field in fields}
    row.update(values)
    return row


def _response(fields: tuple[str, ...], rows: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "hesi-1"},
        json={
            "errCode": "DLM.0",
            "data": {
                "success": True,
                "rowSize": len(rows),
                "columnNames": list(fields),
                "data": rows,
            },
        },
    )


def test_hesi_detail_uses_post_json_paginates_and_accepts_expense_code() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.query == b""
        body = json.loads(request.content)
        offsets.append(body["offsetValue"])
        rows = (
            [
                _raw_row(
                    HESI_DETAIL_FIELDS,
                    company_code="3HD0",
                    expense_code="B1",
                    description="接待1",
                ),
                _raw_row(
                    HESI_DETAIL_FIELDS,
                    company_code="3HD0",
                    expense_code="B2",
                    description="接待2",
                ),
            ]
            if body["offsetValue"] == 0
            else [
                _raw_row(
                    HESI_DETAIL_FIELDS,
                    company_code="3HD0",
                    expense_code="B3",
                    description="接待3",
                )
            ]
        )
        return _response(HESI_DETAIL_FIELDS, rows)

    configuration = HesiDetailClientConfiguration(
        endpoint="https://116.63.221.181/post/hesimingxi",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
        page_size=2,
    )
    with HesiDetailClient(
        configuration,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as client:
        rows = client.fetch_rows(company_code="3HD0")

    assert offsets == [0, 2]
    assert [row.document_code for row in rows] == ["B1", "B2", "B3"]


def test_hesi_detail_accepts_compatible_code_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["offsetValue"] > 0:
            return _response(HESI_DETAIL_CODE_FIELDS, [])
        return _response(
            HESI_DETAIL_CODE_FIELDS,
            [
                _raw_row(
                    HESI_DETAIL_CODE_FIELDS,
                    company_code="3HD0",
                    code="B1",
                    description="接待",
                )
            ],
        )

    configuration = HesiDetailClientConfiguration(
        endpoint="https://116.63.221.181/post/hesimingxi",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
    )
    with HesiDetailClient(configuration, transport=httpx.MockTransport(handler)) as client:
        rows = client.fetch_rows(company_code="3HD0")

    assert rows[0].document_code == "B1"


def test_hesi_invoice_uses_get_with_query_parameters() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.content == b""
        assert request.url.params["company_code"] == "3HD0"
        assert request.url.params["limitValue"] == "15000"
        offset = int(request.url.params["offsetValue"])
        offsets.append(offset)
        if offset >= 2:
            return _response(HESI_INVOICE_FIELDS, [])
        return _response(
            HESI_INVOICE_FIELDS,
            [
                _raw_row(
                    HESI_INVOICE_FIELDS,
                    company_code="3HD0",
                    code="B1",
                    invoice_id="INV-1",
                    reception_apply_code="A1",
                )
            ],
        )

    configuration = HesiInvoiceClientConfiguration(
        endpoint="https://116.63.221.181/post/hesiinvoice",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
    )
    with HesiInvoiceClient(
        configuration,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as client:
        rows = client.fetch_rows(company_code="3HD0")

    assert rows[0].reception_apply_code == "A1"
    assert offsets == [0, 1, 2]
    assert len(rows) == 2


def test_application_uses_post_with_query_and_empty_body() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.content == b""
        assert request.url.params["company_code"] == "3HD0"
        offset = int(request.url.params["offsetValue"])
        offsets.append(offset)
        if offset > 0:
            return _response(HESI_APPLICATION_FIELDS, [])
        return _response(
            HESI_APPLICATION_FIELDS,
            [
                _raw_row(
                    HESI_APPLICATION_FIELDS,
                    company_code="3HD0",
                    code="A1",
                    description="会议通知",
                )
            ],
        )

    configuration = HesiApplicationClientConfiguration(
        endpoint="https://116.63.221.181/post/apply",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
    )
    with HesiApplicationClient(
        configuration,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ) as client:
        rows = client.fetch_rows(company_code="3HD0")

    assert rows[0].description == "会议通知"
    assert offsets == [0, 1]


def test_client_rejects_company_scope_mismatch_and_schema_drift() -> None:
    responses = iter(
        (
            _response(
                HESI_DETAIL_FIELDS,
                [
                    _raw_row(
                        HESI_DETAIL_FIELDS,
                        company_code="OTHER",
                        expense_code="B1",
                    )
                ],
            ),
            _response(
                (*HESI_DETAIL_FIELDS, "unexpected"),
                [
                    _raw_row(
                        (*HESI_DETAIL_FIELDS, "unexpected"),
                        company_code="3HD0",
                        expense_code="B1",
                    )
                ],
            ),
        )
    )

    configuration = HesiDetailClientConfiguration(
        endpoint="https://116.63.221.181/post/hesimingxi",
        app_key=SecretStr("key"),
        app_secret=SecretStr("secret"),
    )
    with HesiDetailClient(
        configuration,
        transport=httpx.MockTransport(lambda _: next(responses)),
    ) as client:
        with pytest.raises(HesiBusinessDataClientError) as company_error:
            client.fetch_rows(company_code="3HD0")
        with pytest.raises(HesiBusinessDataClientError) as schema_error:
            client.fetch_rows(company_code="3HD0")

    assert company_error.value.error_code == "DGC_COMPANY_SCOPE_MISMATCH"
    assert schema_error.value.error_code == "DGC_SCHEMA_DRIFT"

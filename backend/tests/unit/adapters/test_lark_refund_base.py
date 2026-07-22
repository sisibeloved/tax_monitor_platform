from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from tax_risk.adapters.lark.refund_base import (
    LarkRefundApiError,
    LarkRefundAuthenticationError,
    LarkRefundBaseClient,
    LarkRefundBaseConfig,
    LarkRefundDuplicateRecordError,
    LarkRefundHttpError,
    LarkRefundPaginationError,
    LarkRefundPreflightError,
    LarkRefundRateLimitError,
    LarkRefundRecordNotFoundError,
    LarkRefundResponseError,
    LarkRefundTransportError,
    LarkRefundUpdateRejectedError,
    LarkRefundVerificationError,
)


API_ORIGIN = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
SEARCH_PATH = "/open-apis/base/v3/bases/base-sensitive/tables/table-1/records/search"
UPDATE_PATH = "/open-apis/base/v3/bases/base-sensitive/tables/table-1/records/record-1"
COMPANY_FIELD_PATH = (
    "/open-apis/base/v3/bases/base-sensitive/tables/table-1/fields/field-company"
)
STATUS_FIELD_PATH = (
    "/open-apis/base/v3/bases/base-sensitive/tables/table-1/fields/field-status"
)
APP_ID = "app-sensitive"
APP_SECRET = "secret-sensitive"


def _config(**overrides: object) -> LarkRefundBaseConfig:
    values: dict[str, object] = {
        "base_token": "base-sensitive",
        "table_id": "table-1",
        "company_code_field_id": "field-company",
        "status_field_id": "field-status",
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "api_base_url": API_ORIGIN,
        "timeout": 5,
        "page_size": 100,
        "max_pages": 10,
        "token_refresh_margin": 10,
    }
    values.update(overrides)
    return LarkRefundBaseConfig(**values)  # type: ignore[arg-type]


def _token_response(token: str = "tenant-sensitive", *, expire: int = 7_200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": token,
            "expire": expire,
        },
    )


def _search_response(
    rows: list[list[object]],
    *,
    record_ids: list[str] | None = None,
    field_ids: list[str] | None = None,
    has_more: bool = False,
) -> httpx.Response:
    data: dict[str, object] = {
        "data": rows,
        "field_id_list": (
            field_ids if field_ids is not None else ["field-company", "field-status"]
        ),
        "record_id_list": (
            record_ids
            if record_ids is not None
            else [f"record-{index + 1}" for index in range(len(rows))]
        ),
        "has_more": has_more,
    }
    return httpx.Response(200, json={"code": 0, "msg": "ok", "data": data})


def _field_response(field_id: str, **metadata: object) -> httpx.Response:
    field: dict[str, object] = {
        "id": field_id,
        "name": field_id,
        "type": "text",
    }
    field.update(metadata)
    return httpx.Response(
        200,
        json={"code": 0, "msg": "ok", "data": {"field": field}},
    )


def test_client_caches_tenant_token_until_refresh_window() -> None:
    now = 0.0
    token_calls = 0
    search_authorizations: list[str] = []

    def clock() -> float:
        return now

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            body = json.loads(request.content)
            assert body == {"app_id": APP_ID, "app_secret": APP_SECRET}
            return _token_response(f"tenant-{token_calls}", expire=100)
        assert request.url.path == SEARCH_PATH
        search_authorizations.append(request.headers["Authorization"])
        return _search_response([["3000", "已退税"]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
    )

    client.write_status(company_code="3000", desired_value="已退税")
    now = 89
    client.write_status(company_code="3000", desired_value="已退税")
    now = 91
    client.write_status(company_code="3000", desired_value="已退税")

    assert token_calls == 2
    assert search_authorizations == ["Bearer tenant-1", "Bearer tenant-1", "Bearer tenant-2"]


def test_client_scans_all_pages_then_updates_the_unique_exact_record() -> None:
    requests: list[httpx.Request] = []
    persisted_status: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal persisted_status
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.method == "POST" and request.url.path == SEARCH_PATH:
            body = json.loads(request.content)
            assert body["keyword"] == "3000"
            assert body["search_fields"] == ["field-company"]
            assert body["select_fields"] == ["field-company", "field-status"]
            assert body["limit"] == 1
            if body["offset"] == 0:
                return _search_response(
                    [["30000", "未退税"]],
                    record_ids=["record-near-match"],
                    has_more=True,
                )
            assert body["offset"] == 1
            return _search_response(
                [[[{"text": "3000"}], persisted_status or "未退税"]],
                record_ids=["record-1"],
            )
        assert request.method == "PATCH"
        assert request.url.path == UPDATE_PATH
        assert request.headers["Authorization"] == "Bearer tenant-sensitive"
        update = json.loads(request.content)
        assert update == {"field-status": "已退税"}
        persisted_status = update["field-status"]
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

    client = LarkRefundBaseClient(
        _config(page_size=1),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.write_status(company_code=" 3000 ", desired_value=" 已退税 ")

    assert result.company_code == "3000"
    assert result.record_id == "record-1"
    assert result.previous_value == "未退税"
    assert result.desired_value == "已退税"
    assert result.updated is True
    assert [request.method for request in requests] == [
        "POST",
        "POST",
        "POST",
        "PATCH",
        "POST",
        "POST",
    ]


def test_client_is_idempotent_when_status_already_matches() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return _search_response([["3000", [{"text": "已退税"}]]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.write_status(company_code="3000", desired_value="已退税")

    assert result.updated is False
    assert result.previous_value == "已退税"
    assert methods == ["POST", "POST"]


def test_client_rejects_zero_exact_matches_without_updating() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return _search_response([["30000", "未退税"]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundRecordNotFoundError) as raised:
        client.write_status(company_code="3000", desired_value="已退税")

    assert raised.value.error_code == "LARK_REFUND_RECORD_NOT_FOUND"
    assert methods == ["POST", "POST"]


def test_client_rejects_multiple_exact_matches_across_pages_without_updating() -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == TOKEN_PATH:
            return _token_response()
        assert request.method == "POST"
        search_calls += 1
        if search_calls == 1:
            return _search_response(
                [["3000", "未退税"]],
                record_ids=["record-1"],
                has_more=True,
            )
        return _search_response(
            [["3000", "未退税"]],
            record_ids=["record-2"],
        )

    client = LarkRefundBaseClient(
        _config(page_size=1),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundDuplicateRecordError) as raised:
        client.write_status(company_code="3000", desired_value="已退税")

    assert raised.value.error_code == "LARK_REFUND_DUPLICATE_RECORD"
    assert search_calls == 2


def test_authentication_api_error_does_not_expose_credentials_or_response_message() -> None:
    leaked_token = "token-returned-in-sensitive-message"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 10003,
                "msg": f"bad {APP_ID} {APP_SECRET} {leaked_token}",
            },
        )

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundAuthenticationError) as raised:
        client.write_status(company_code="3000", desired_value="已退税")

    message = str(raised.value)
    assert raised.value.error_code == "LARK_REFUND_AUTHENTICATION_FAILED"
    assert "10003" in message
    assert APP_ID not in message
    assert APP_SECRET not in message
    assert leaked_token not in message
    assert APP_ID not in repr(_config())
    assert APP_SECRET not in repr(_config())
    assert "base-sensitive" not in repr(_config())


def test_record_api_and_http_errors_are_stable_and_redacted() -> None:
    access_token = "tenant-sensitive-access-token"
    leaked_message = f"bad request: {APP_SECRET} {access_token} base-sensitive"
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == TOKEN_PATH:
            return _token_response(access_token)
        search_calls += 1
        if search_calls == 1:
            return httpx.Response(200, json={"code": 1254001, "msg": leaked_message})
        return httpx.Response(503, text=leaked_message)

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundApiError) as api_raised:
        client.write_status(company_code="3000", desired_value="已退税")
    with pytest.raises(LarkRefundHttpError) as http_raised:
        client.write_status(company_code="3000", desired_value="已退税")

    for error in (api_raised.value, http_raised.value):
        message = str(error)
        assert APP_SECRET not in message
        assert access_token not in message
        assert "base-sensitive" not in message
        assert leaked_message not in message


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        pytest.param({"base_token": ""}, ValueError, id="empty-base-token"),
        pytest.param({"table_id": "table/escape"}, ValueError, id="unsafe-path-segment"),
        pytest.param({"app_secret": "  "}, ValueError, id="empty-app-secret"),
        pytest.param(
            {"status_field_id": "field-company"},
            ValueError,
            id="same-business-and-status-field",
        ),
        pytest.param(
            {"api_base_url": "http://open.feishu.cn"},
            ValueError,
            id="insecure-api-origin",
        ),
        pytest.param(
            {"api_base_url": "https://open.feishu.cn/other"},
            ValueError,
            id="api-origin-with-path",
        ),
        pytest.param({"timeout": True}, TypeError, id="boolean-timeout"),
        pytest.param({"timeout": 0}, ValueError, id="nonpositive-timeout"),
        pytest.param({"page_size": 201}, ValueError, id="oversized-page"),
        pytest.param({"page_size": 1.5}, TypeError, id="noninteger-page-size"),
        pytest.param({"max_pages": False}, TypeError, id="boolean-max-pages"),
        pytest.param({"max_pages": 0}, ValueError, id="nonpositive-max-pages"),
        pytest.param(
            {"token_refresh_margin": False},
            TypeError,
            id="boolean-refresh-margin",
        ),
        pytest.param(
            {"token_refresh_margin": -1},
            ValueError,
            id="negative-refresh-margin",
        ),
        pytest.param(
            {"allow_untrusted_api_origin": 1},
            TypeError,
            id="nonboolean-origin-override",
        ),
    ],
)
def test_configuration_rejects_unsafe_or_contradictory_values(
    overrides: dict[str, object],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        _config(**overrides)


def test_client_config_requires_an_explicit_override_for_a_mock_api_origin() -> None:
    with pytest.raises(ValueError, match="explicit test override"):
        _config(api_base_url="https://open.feishu.mock.test")

    config = _config(
        api_base_url="https://open.feishu.mock.test",
        allow_untrusted_api_origin=True,
    )

    assert config.api_base_url == "https://open.feishu.mock.test"
    assert config.allow_untrusted_api_origin is True


@pytest.mark.parametrize(
    ("company_code", "desired_value"),
    [
        ("", "RECEIVED"),
        ("3000", "  "),
    ],
)
def test_write_status_rejects_empty_business_values_before_network_call(
    company_code: str,
    desired_value: str,
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid input must not reach the network")

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(unexpected_request)),
    )

    with pytest.raises(ValueError):
        client.write_status(company_code, desired_value)


def _valid_search_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "data": [["3000", "RECEIVED"]],
        "field_id_list": ["field-company", "field-status"],
        "record_id_list": ["record-1"],
        "has_more": False,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "search_data",
    [
        pytest.param(None, id="missing-data-object"),
        pytest.param(
            _valid_search_data(
                field_id_list=["field-company", "field-company", "field-status"]
            ),
            id="duplicate-projection",
        ),
        pytest.param(
            _valid_search_data(field_id_list=["field-company"]),
            id="missing-status-projection",
        ),
        pytest.param(
            _valid_search_data(field_id_list=["field-company", 7]),
            id="nontext-projection-id",
        ),
        pytest.param(_valid_search_data(data="not-a-list"), id="rows-not-a-list"),
        pytest.param(
            _valid_search_data(record_id_list=[]),
            id="row-and-record-id-count-mismatch",
        ),
        pytest.param(
            _valid_search_data(data=[["3000"]]),
            id="incomplete-projected-row",
        ),
        pytest.param(
            _valid_search_data(record_id_list=["record/escape"]),
            id="unsafe-record-id",
        ),
        pytest.param(_valid_search_data(has_more="false"), id="nonboolean-has-more"),
    ],
)
def test_search_response_must_prove_a_complete_unique_record_shape(
    search_data: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": search_data})

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundResponseError):
        client.write_status("3000", "RECEIVED")


def test_search_rejects_an_empty_page_that_claims_more_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return _search_response([], record_ids=[], has_more=True)

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundPaginationError, match="empty page"):
        client.write_status("3000", "RECEIVED")


def test_search_rejects_a_record_repeated_on_a_later_page() -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == TOKEN_PATH:
            return _token_response()
        search_calls += 1
        return _search_response(
            [["near-match", "PENDING"]],
            record_ids=["record-repeated"],
            has_more=search_calls == 1,
        )

    client = LarkRefundBaseClient(
        _config(page_size=1),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundPaginationError, match="repeated a record"):
        client.write_status("3000", "RECEIVED")

    assert search_calls == 2


def test_search_fails_closed_when_maximum_page_count_cannot_prove_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return _search_response(
            [["near-match", "PENDING"]],
            record_ids=["record-1"],
            has_more=True,
        )

    client = LarkRefundBaseClient(
        _config(page_size=1, max_pages=1),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundPaginationError, match="page limit"):
        client.write_status("3000", "RECEIVED")


@pytest.mark.parametrize(
    ("patch_status", "patch_payload", "error_type"),
    [
        pytest.param(503, {"secret": APP_SECRET}, LarkRefundHttpError, id="http-error"),
        pytest.param(
            200,
            {"code": 1254001, "msg": APP_SECRET},
            LarkRefundApiError,
            id="api-error",
        ),
    ],
)
def test_patch_failure_is_reported_without_returning_success(
    patch_status: int,
    patch_payload: dict[str, object],
    error_type: type[Exception],
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.method == "POST":
            return _search_response([["3000", "PENDING"]])
        return httpx.Response(patch_status, json=patch_payload)

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(error_type) as raised:
        client.write_status("3000", "RECEIVED")

    assert methods == ["POST", "POST", "PATCH"]
    assert APP_SECRET not in str(raised.value)


def test_transport_failure_is_stable_and_does_not_chain_sensitive_details() -> None:
    leaked_detail = f"connection failed with {APP_SECRET}"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(leaked_detail, request=request)

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundTransportError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.error_code == "LARK_REFUND_TRANSPORT_FAILED"
    assert APP_SECRET not in str(raised.value)
    assert raised.value.__cause__ is None


def test_invalid_json_is_rejected_without_echoing_response_content() -> None:
    leaked_content = f"not-json {APP_SECRET}".encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=leaked_content)

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundResponseError, match="invalid JSON") as raised:
        client.write_status("3000", "RECEIVED")

    assert APP_SECRET not in str(raised.value)


@pytest.mark.parametrize(
    ("token_payload", "error_type"),
    [
        pytest.param(
            {"code": 0, "expire": 7_200},
            LarkRefundAuthenticationError,
            id="missing-token",
        ),
        pytest.param(
            {"code": 0, "tenant_access_token": "token", "expire": 0},
            LarkRefundAuthenticationError,
            id="invalid-expiry",
        ),
        pytest.param(
            {"code": "0", "tenant_access_token": "token", "expire": 7_200},
            LarkRefundResponseError,
            id="invalid-api-code-type",
        ),
    ],
)
def test_authentication_response_requires_complete_typed_token_metadata(
    token_payload: dict[str, object],
    error_type: type[Exception],
) -> None:
    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=token_payload)
            )
        ),
    )

    with pytest.raises(error_type):
        client.write_status("3000", "RECEIVED")


def test_close_only_closes_an_owned_http_client() -> None:
    external_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: _token_response())
    )
    injected_adapter = LarkRefundBaseClient(_config(), external_client)

    with injected_adapter as entered:
        assert entered is injected_adapter

    assert external_client.is_closed is False
    external_client.close()

    owned_adapter = LarkRefundBaseClient(_config())
    owned_http_client = owned_adapter._client
    with owned_adapter as entered:
        assert entered is owned_adapter

    assert owned_http_client.is_closed is True


@pytest.mark.parametrize("rejected_status", [401, 403])
def test_base_authentication_rejection_refreshes_the_token_and_replays_once(
    rejected_status: int,
) -> None:
    token_calls = 0
    search_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            return _token_response(f"tenant-{token_calls}")
        authorization = request.headers["Authorization"]
        search_authorizations.append(authorization)
        if authorization == "Bearer tenant-1":
            return httpx.Response(rejected_status, json={"code": 99991663})
        return _search_response([["3000", "RECEIVED"]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.write_status("3000", "RECEIVED")

    assert result.updated is False
    assert token_calls == 2
    assert search_authorizations == ["Bearer tenant-1", "Bearer tenant-2"]


def test_persistent_authentication_rejection_stops_after_one_replay() -> None:
    token_calls = 0
    base_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, base_calls
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            return _token_response(f"tenant-{token_calls}")
        base_calls += 1
        return httpx.Response(401, json={"code": 99991663})

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundHttpError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.status_code == 401
    assert token_calls == 2
    assert base_calls == 2


def test_patch_authentication_rejection_replays_then_confirms_the_update() -> None:
    token_calls = 0
    patch_calls = 0
    persisted_status = "PENDING"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, patch_calls, persisted_status
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            return _token_response(f"tenant-{token_calls}")
        if request.method == "POST":
            return _search_response([["3000", persisted_status]])
        patch_calls += 1
        if patch_calls == 1:
            assert request.headers["Authorization"] == "Bearer tenant-1"
            return httpx.Response(401, json={"code": 99991663})
        assert request.headers["Authorization"] == "Bearer tenant-2"
        persisted_status = json.loads(request.content)["field-status"]
        return httpx.Response(
            200,
            json={"code": 0, "data": {"updated": True, "ignored_fields": []}},
        )

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.write_status("3000", "RECEIVED")

    assert result.updated is True
    assert persisted_status == "RECEIVED"
    assert token_calls == 2
    assert patch_calls == 2


@pytest.mark.parametrize(
    ("retry_after_header", "expected_retry_after"),
    [
        ("17", 17.0),
        ("1.5", 1.5),
        ("not-a-delay", None),
        ("-1", None),
        (None, None),
    ],
)
def test_rate_limit_error_exposes_a_sanitized_retry_after(
    retry_after_header: str | None,
    expected_retry_after: float | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return httpx.Response(
            429,
            headers=(
                {"Retry-After": retry_after_header}
                if retry_after_header is not None
                else {}
            ),
            text=f"sensitive {APP_SECRET}",
        )

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundRateLimitError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.error_code == "LARK_REFUND_RATE_LIMITED"
    assert raised.value.status_code == 429
    assert raised.value.retry_after == expected_retry_after
    assert raised.value.retry_after_seconds == expected_retry_after
    assert APP_SECRET not in str(raised.value)


def test_rate_limit_error_parses_an_rfc_http_date_with_an_injected_wall_clock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return httpx.Response(
            429,
            headers={"Retry-After": "Thu, 01 Jan 2026 00:00:30 GMT"},
        )

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
        wall_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(LarkRefundRateLimitError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.retry_after_seconds == 30.0


@pytest.mark.parametrize(
    "update_data",
    [
        pytest.param(
            {"ignored_fields": [{"field_id": "field-status", "reason": "READONLY"}]},
            id="ignored-field",
        ),
        pytest.param(
            {"record": {"failed_fields": ["field-status"]}},
            id="nested-failed-field",
        ),
        pytest.param({"errors": ["write rejected"]}, id="errors"),
        pytest.param({"failures": {"field-status": "rejected"}}, id="failures"),
        pytest.param({"updated": False}, id="updated-false"),
        pytest.param({"success": False}, id="success-false"),
    ],
)
def test_update_failure_metadata_is_rejected_before_readback(
    update_data: dict[str, object],
) -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.method == "POST":
            search_calls += 1
            return _search_response([["3000", "PENDING"]])
        return httpx.Response(
            200,
            json={"code": 0, "data": update_data, "msg": APP_SECRET},
        )

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundUpdateRejectedError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.error_code == "LARK_REFUND_UPDATE_REJECTED"
    assert search_calls == 1
    assert APP_SECRET not in str(raised.value)


def test_update_with_invalid_metadata_shape_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.method == "POST":
            return _search_response([["3000", "PENDING"]])
        return httpx.Response(200, json={"code": 0, "data": []})

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundResponseError, match="invalid response metadata"):
        client.write_status("3000", "RECEIVED")


def test_code_zero_update_is_not_success_until_readback_confirms_the_value() -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.method == "POST":
            search_calls += 1
            return _search_response([["3000", "PENDING"]])
        return httpx.Response(200, json={"code": 0, "data": {"updated": True}})

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundVerificationError) as raised:
        client.write_status("3000", "RECEIVED")

    assert raised.value.error_code == "LARK_REFUND_UPDATE_NOT_CONFIRMED"
    assert search_calls == 2


def test_read_status_uses_the_exact_unique_record_contract_without_writing() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        return _search_response([["3000", "PENDING"]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.read_status("3000") == "PENDING"
    assert methods == ["POST", "POST"]


def test_preflight_validates_field_schema_and_app_record_read_access() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        assert request.headers["Authorization"] == "Bearer tenant-sensitive"
        if request.url.path == COMPANY_FIELD_PATH:
            assert request.method == "GET"
            assert request.content == b""
            return _field_response("field-company", readable=True)
        if request.url.path == STATUS_FIELD_PATH:
            assert request.method == "GET"
            assert request.content == b""
            return _field_response("field-status", readable=True, writable=True)
        assert request.url.path == SEARCH_PATH
        return _search_response([["3000", "PENDING"]])

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.preflight("3000")

    assert result.company_code == "3000"
    assert result.record_id == "record-1"
    assert result.company_code_field_type == "text"
    assert result.status_field_type == "text"
    assert result.status_value == "PENDING"
    assert [request.method for request in requests] == ["POST", "GET", "GET", "POST"]


def test_schema_preflight_is_cached_for_the_client_process() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.url.path == COMPANY_FIELD_PATH:
            return _field_response("field-company", readable=True)
        if request.url.path == STATUS_FIELD_PATH:
            return _field_response("field-status", readable=True, writable=True)
        raise AssertionError("schema-only preflight must not search or update records")

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.ensure_schema() == ("text", "text")
    assert client.ensure_schema() == ("text", "text")
    assert requested_paths == [TOKEN_PATH, COMPANY_FIELD_PATH, STATUS_FIELD_PATH]


@pytest.mark.parametrize(
    ("target_path", "metadata"),
    [
        pytest.param(
            COMPANY_FIELD_PATH,
            {"id": "different-field"},
            id="company-field-id-mismatch",
        ),
        pytest.param(COMPANY_FIELD_PATH, {"type": "number"}, id="company-field-not-text"),
        pytest.param(
            COMPANY_FIELD_PATH,
            {"readable": False},
            id="company-field-not-readable",
        ),
        pytest.param(STATUS_FIELD_PATH, {"type": "formula"}, id="status-field-read-only-type"),
        pytest.param(
            STATUS_FIELD_PATH,
            {"writable": False},
            id="status-field-not-writable",
        ),
        pytest.param(
            STATUS_FIELD_PATH,
            {"is_read_only": True},
            id="status-field-explicitly-read-only",
        ),
    ],
)
def test_preflight_rejects_field_drift_or_read_only_metadata(
    target_path: str,
    metadata: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response()
        if request.url.path == COMPANY_FIELD_PATH:
            return _field_response("field-company", **(metadata if target_path == request.url.path else {}))
        if request.url.path == STATUS_FIELD_PATH:
            return _field_response("field-status", **(metadata if target_path == request.url.path else {}))
        raise AssertionError("invalid field metadata must fail before record search")

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundPreflightError) as raised:
        client.preflight("3000")

    assert raised.value.error_code == "LARK_REFUND_PREFLIGHT_FAILED"


def test_preflight_fails_when_the_app_cannot_read_field_metadata() -> None:
    token_calls = 0
    field_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, field_calls
        if request.url.path == TOKEN_PATH:
            token_calls += 1
            return _token_response(f"tenant-{token_calls}")
        field_calls += 1
        return httpx.Response(403, json={"code": 1254302})

    client = LarkRefundBaseClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LarkRefundHttpError) as raised:
        client.preflight("3000")

    assert raised.value.status_code == 403
    assert token_calls == 2
    assert field_calls == 2

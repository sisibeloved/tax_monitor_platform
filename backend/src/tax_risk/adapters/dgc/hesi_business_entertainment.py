from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import hmac
import json
import ssl
from typing import Generic, NoReturn, TypeVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from tax_risk.application.tax_adjustment_accounts.business_entertainment import (
    HesiApplicationRow,
    HesiDetailRow,
    HesiInvoiceRow,
)


HESI_DETAIL_FIELDS = (
    "company_id",
    "company_code",
    "company_name",
    "employee_code",
    "employee_name",
    "expense_code",
    "template_id",
    "template_name",
    "title",
    "department_code",
    "department_name",
    "business_scope_id",
    "business_scope_code",
    "business_scope_name",
    "project_id",
    "project_code",
    "project_name",
    "submit_date",
    "flow_end_date",
    "total_reimbursement_amount",
    "description",
    "fee_type_code",
    "fee_type_name",
    "fee_type_amount",
    "payee_id",
    "payee_name",
    "voucher_no",
    "flow_id",
    "flow_title",
    "flow_no",
    "flow_type",
    "shared_accountant_id",
    "shared_accountant_code",
    "shared_accountant_name",
    "shared_accountant_comment",
    "shared_accountant_complete_time",
    "shared_accountant_start_time",
)
HESI_DETAIL_CODE_FIELDS = tuple(
    "code" if field == "expense_code" else field for field in HESI_DETAIL_FIELDS
)
HESI_INVOICE_FIELDS = (
    "code",
    "expense_link",
    "reception_apply_code",
    "feetypeid",
    "invoice_type",
    "invoice_confirm",
    "invoice_id",
    "amount_standard_dec",
    "tax_amount_standard_dec",
    "approve_amount_dec",
    "company_code",
    "company_name",
)
HESI_APPLICATION_FIELDS = (
    "code",
    "form_specification_name",
    "u_oa_number",
    "user_name",
    "requisition_time",
    "company_name",
    "expense_department_name",
    "pro_name",
    "title",
    "description",
    "requisition_money_standard",
    "reception_standard_name",
    "reception_type_name",
    "create_time",
    "update_time",
    "active",
    "u_flow_id",
    "company_code",
)


class _CompanyDataClientConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    endpoint: str
    app_key: SecretStr
    app_secret: SecretStr
    page_size: int = Field(default=15_000, gt=0, le=15_000)
    max_records: int = Field(default=500_000, gt=0)
    max_pages: int = Field(default=100, gt=0)
    max_page_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=240, gt=0, le=600)
    tls_server_name: str | None = None

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_plain_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("DGC endpoint must use HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("DGC endpoint cannot contain query or fragment")
        return value.rstrip("/")

    @field_validator("tls_server_name")
    @classmethod
    def normalize_tls_server_name(cls, value: str | None) -> str | None:
        return value.strip().lower() or None if value is not None else None


class HesiDetailClientConfiguration(_CompanyDataClientConfiguration):
    pass


class HesiInvoiceClientConfiguration(_CompanyDataClientConfiguration):
    pass


class HesiApplicationClientConfiguration(_CompanyDataClientConfiguration):
    pass


class HesiBusinessDataClientError(RuntimeError):
    def __init__(
        self,
        dataset: str,
        error_code: str,
        *,
        request_id: str | None = None,
    ) -> None:
        self.dataset = dataset
        self.error_code = error_code
        self.request_id = request_id
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"{dataset} failed: {error_code}{suffix}")


class _RequestProtocol(StrEnum):
    POST_JSON = "POST_JSON"
    GET_QUERY = "GET_QUERY"
    POST_QUERY = "POST_QUERY"


RowT = TypeVar("RowT")


class _CompanyDataClient(Generic[RowT]):
    def __init__(
        self,
        configuration: _CompanyDataClientConfiguration,
        *,
        dataset: str,
        request_protocol: _RequestProtocol,
        schemas: tuple[tuple[str, ...], ...],
        parser: Callable[[Mapping[str, object]], RowT],
        transport: httpx.BaseTransport | None,
        verify: ssl.SSLContext | bool,
        clock: Callable[[], datetime] | None,
    ) -> None:
        self._configuration = configuration
        self._dataset = dataset
        self._request_protocol = request_protocol
        self._schema_sets = tuple(frozenset(schema) for schema in schemas)
        self._parser = parser
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client = httpx.Client(
            transport=transport,
            verify=verify,
            timeout=configuration.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def fetch_rows(self, *, company_code: str) -> tuple[RowT, ...]:
        company = company_code.strip()
        if not company:
            raise ValueError("company_code is required")

        rows: list[RowT] = []
        offset = 0
        effective_page_size = self._configuration.page_size
        for _ in range(self._configuration.max_pages):
            page_rows = self._fetch_page(company_code=company, offset=offset)
            if not page_rows:
                return tuple(rows)
            rows.extend(page_rows)
            if len(rows) > self._configuration.max_records:
                self._raise("DGC_RECORD_LIMIT_EXCEEDED")
            if offset > 0 and len(page_rows) < effective_page_size:
                return tuple(rows)
            effective_page_size = min(effective_page_size, len(page_rows))
            offset += len(page_rows)
        self._raise("DGC_PAGE_LIMIT_EXCEEDED")

    def _fetch_page(self, *, company_code: str, offset: int) -> tuple[RowT, ...]:
        parameters: dict[str, str | int] = {
            "company_code": company_code,
            "offsetValue": offset,
            "limitValue": self._configuration.page_size,
        }
        if self._request_protocol is _RequestProtocol.POST_JSON:
            method = "POST"
            query = ""
            body = json.dumps(
                parameters,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_url = self._configuration.endpoint
        else:
            method = "GET" if self._request_protocol is _RequestProtocol.GET_QUERY else "POST"
            query = _canonical_query(parameters)
            body = b""
            request_url = f"{self._configuration.endpoint}?{query}"

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("DGC signing clock must return a timezone-aware datetime")
        sdk_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = _build_apig_headers(
            endpoint=self._configuration.endpoint,
            method=method,
            canonical_query=query,
            app_key=self._configuration.app_key.get_secret_value(),
            app_secret=self._configuration.app_secret.get_secret_value(),
            body=body,
            sdk_date=sdk_date,
        )
        response = self._client.request(
            method,
            request_url,
            content=body,
            headers=headers,
            extensions=(
                {"sni_hostname": self._configuration.tls_server_name}
                if self._configuration.tls_server_name is not None
                else None
            ),
        )
        request_id = response.headers.get("x-request-id")
        if len(response.content) > self._configuration.max_page_bytes:
            self._raise("DGC_PAGE_BYTES_EXCEEDED", request_id=request_id)
        try:
            payload = json.loads(response.content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._raise("DGC_RESPONSE_INVALID_JSON", request_id=request_id, cause=error)
        if not isinstance(payload, dict):
            self._raise("DGC_RESPONSE_INVALID_SHAPE", request_id=request_id)
        if payload.get("errCode") != "DLM.0":
            self._raise(
                str(payload.get("errCode") or f"HTTP_{response.status_code}"),
                request_id=request_id,
            )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            self._raise("DGC_RESPONSE_NOT_SUCCESSFUL", request_id=request_id)
        return self._parse_page(data, company_code=company_code, request_id=request_id)

    def _parse_page(
        self,
        data: dict[str, object],
        *,
        company_code: str,
        request_id: str | None,
    ) -> tuple[RowT, ...]:
        raw_columns = data.get("columnNames")
        raw_rows = data.get("data")
        row_size = _nonnegative_int(data.get("rowSize"), self._dataset, request_id)
        if not isinstance(raw_columns, list) or not all(
            isinstance(column, str) for column in raw_columns
        ):
            self._raise("DGC_COLUMNS_INVALID", request_id=request_id)
        columns = frozenset(raw_columns)
        if columns not in self._schema_sets:
            self._raise("DGC_SCHEMA_DRIFT", request_id=request_id)
        if not isinstance(raw_rows, list) or row_size != len(raw_rows):
            self._raise("DGC_ROW_COUNT_MISMATCH", request_id=request_id)

        parsed_rows: list[RowT] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict) or frozenset(raw_row) != columns:
                self._raise("DGC_ROW_SCHEMA_DRIFT", request_id=request_id)
            try:
                row = self._parser(raw_row)
            except ValueError as error:
                self._raise("DGC_ROW_VALIDATION_FAILED", request_id=request_id, cause=error)
            row_company = getattr(row, "company_code", None)
            if row_company != company_code:
                self._raise("DGC_COMPANY_SCOPE_MISMATCH", request_id=request_id)
            parsed_rows.append(row)
        return tuple(parsed_rows)

    def _raise(
        self,
        error_code: str,
        *,
        request_id: str | None = None,
        cause: BaseException | None = None,
    ) -> NoReturn:
        error = HesiBusinessDataClientError(
            self._dataset,
            error_code,
            request_id=request_id,
        )
        if cause is None:
            raise error
        raise error from cause


class HesiDetailClient:
    def __init__(
        self,
        configuration: HesiDetailClientConfiguration,
        *,
        transport: httpx.BaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._delegate = _CompanyDataClient[HesiDetailRow](
            configuration,
            dataset="hesimingxi",
            request_protocol=_RequestProtocol.POST_JSON,
            schemas=(HESI_DETAIL_FIELDS, HESI_DETAIL_CODE_FIELDS),
            parser=_parse_detail_row,
            transport=transport,
            verify=verify,
            clock=clock,
        )

    def __enter__(self) -> HesiDetailClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._delegate.close()

    def fetch_rows(self, *, company_code: str) -> tuple[HesiDetailRow, ...]:
        return self._delegate.fetch_rows(company_code=company_code)


class HesiInvoiceClient:
    def __init__(
        self,
        configuration: HesiInvoiceClientConfiguration,
        *,
        transport: httpx.BaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._delegate = _CompanyDataClient[HesiInvoiceRow](
            configuration,
            dataset="hesiinvoice",
            request_protocol=_RequestProtocol.GET_QUERY,
            schemas=(HESI_INVOICE_FIELDS,),
            parser=_parse_invoice_row,
            transport=transport,
            verify=verify,
            clock=clock,
        )

    def __enter__(self) -> HesiInvoiceClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._delegate.close()

    def fetch_rows(self, *, company_code: str) -> tuple[HesiInvoiceRow, ...]:
        return self._delegate.fetch_rows(company_code=company_code)


class HesiApplicationClient:
    def __init__(
        self,
        configuration: HesiApplicationClientConfiguration,
        *,
        transport: httpx.BaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._delegate = _CompanyDataClient[HesiApplicationRow](
            configuration,
            dataset="apply",
            request_protocol=_RequestProtocol.POST_QUERY,
            schemas=(HESI_APPLICATION_FIELDS,),
            parser=_parse_application_row,
            transport=transport,
            verify=verify,
            clock=clock,
        )

    def __enter__(self) -> HesiApplicationClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._delegate.close()

    def fetch_rows(self, *, company_code: str) -> tuple[HesiApplicationRow, ...]:
        return self._delegate.fetch_rows(company_code=company_code)


def _parse_detail_row(raw_row: Mapping[str, object]) -> HesiDetailRow:
    document_code = raw_row.get("expense_code", raw_row.get("code"))
    return HesiDetailRow.model_validate(
        {
            "company_code": raw_row.get("company_code"),
            "document_code": document_code,
            "description": raw_row.get("description"),
        }
    )


def _parse_invoice_row(raw_row: Mapping[str, object]) -> HesiInvoiceRow:
    return HesiInvoiceRow.model_validate(
        {
            "company_code": raw_row.get("company_code"),
            "code": raw_row.get("code"),
            "invoice_id": raw_row.get("invoice_id"),
            "reception_apply_code": raw_row.get("reception_apply_code"),
        }
    )


def _parse_application_row(raw_row: Mapping[str, object]) -> HesiApplicationRow:
    return HesiApplicationRow.model_validate(
        {
            "company_code": raw_row.get("company_code"),
            "code": raw_row.get("code"),
            "description": raw_row.get("description"),
        }
    )


def _canonical_query(parameters: Mapping[str, str | int]) -> str:
    encoded = sorted(
        (
            quote(str(key), safe="-_.~"),
            quote(str(value), safe="-_.~"),
        )
        for key, value in parameters.items()
    )
    return "&".join(f"{key}={value}" for key, value in encoded)


def _build_apig_headers(
    *,
    endpoint: str,
    method: str,
    canonical_query: str,
    app_key: str,
    app_secret: str,
    body: bytes,
    sdk_date: str,
) -> dict[str, str]:
    parsed = urlsplit(endpoint)
    canonical_uri = quote(parsed.path or "/", safe="/-_.~")
    if not canonical_uri.endswith("/"):
        canonical_uri += "/"
    signed_headers = "content-type;host;x-sdk-date"
    canonical_headers = (
        f"content-type:application/json\nhost:{parsed.netloc}\nx-sdk-date:{sdk_date}\n"
    )
    canonical_request = "\n".join(
        (
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            sha256(body).hexdigest(),
        )
    )
    string_to_sign = "\n".join(
        (
            "SDK-HMAC-SHA256",
            sdk_date,
            sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    signature = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        sha256,
    ).hexdigest()
    authorization = (
        f"SDK-HMAC-SHA256 Access={app_key}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": parsed.netloc,
        "X-Sdk-Date": sdk_date,
        "Authorization": authorization,
        "x-Authorization": authorization,
        "Content-Type": "application/json",
    }


def _nonnegative_int(value: object, dataset: str, request_id: str | None) -> int:
    if isinstance(value, bool):
        raise HesiBusinessDataClientError(
            dataset,
            "DGC_ROW_SIZE_INVALID",
            request_id=request_id,
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as error:
            raise HesiBusinessDataClientError(
                dataset,
                "DGC_ROW_SIZE_INVALID",
                request_id=request_id,
            ) from error
    else:
        raise HesiBusinessDataClientError(
            dataset,
            "DGC_ROW_SIZE_INVALID",
            request_id=request_id,
        )
    if parsed < 0:
        raise HesiBusinessDataClientError(
            dataset,
            "DGC_ROW_SIZE_INVALID",
            request_id=request_id,
        )
    return parsed


__all__ = [
    "HESI_APPLICATION_FIELDS",
    "HESI_DETAIL_CODE_FIELDS",
    "HESI_DETAIL_FIELDS",
    "HESI_INVOICE_FIELDS",
    "HesiApplicationClient",
    "HesiApplicationClientConfiguration",
    "HesiBusinessDataClientError",
    "HesiDetailClient",
    "HesiDetailClientConfiguration",
    "HesiInvoiceClient",
    "HesiInvoiceClientConfiguration",
]

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
import ssl
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from tax_risk.application.tax_adjustment_accounts.contracts import SettlementAdjustmentRow


SCOPED_FIELDS = (
    "fiscal_year",
    "fiscal_period",
    "voucher_no",
    "header_text",
    "detail_text",
    "amount_ksl",
    "gl_account",
    "account_name",
    "project_code",
    "project_name",
    "debit_credit_flag",
    "group_currency",
    "original_system_doc_no",
)
FULL_FIELDS = ("company", "companyname", *SCOPED_FIELDS)


class SettlementAdjustmentClientConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    endpoint: str
    app_key: SecretStr
    app_secret: SecretStr
    page_size: int = Field(default=15_000, gt=0, le=15_000)
    max_records: int = Field(default=100_000, gt=0)
    max_pages: int = Field(default=100, gt=0)
    max_page_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=240, gt=0, le=600)

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_plain_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("settlement_adjustment endpoint must use HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("settlement_adjustment endpoint cannot contain query or fragment")
        return value.rstrip("/")


class SettlementAdjustmentClientError(RuntimeError):
    def __init__(self, error_code: str, *, request_id: str | None = None) -> None:
        self.error_code = error_code
        self.request_id = request_id
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"settlement_adjustment failed: {error_code}{suffix}")


def build_apig_headers(
    *,
    endpoint: str,
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
            "POST",
            canonical_uri,
            "",
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


class SettlementAdjustmentClient:
    def __init__(
        self,
        configuration: SettlementAdjustmentClientConfiguration,
        *,
        transport: httpx.BaseTransport | None = None,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._configuration = configuration
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client = httpx.Client(
            transport=transport,
            verify=verify,
            timeout=configuration.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> SettlementAdjustmentClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> tuple[SettlementAdjustmentRow, ...]:
        normalized_company = company.strip()
        if not normalized_company:
            raise ValueError("company is required")
        if len(fiscal_year) != 4 or not fiscal_year.isdigit():
            raise ValueError("fiscal_year must contain four digits")

        rows: list[SettlementAdjustmentRow] = []
        offset = 0
        expected_total: int | None = None
        for _ in range(self._configuration.max_pages):
            page_rows, total_size = self._fetch_page(
                company=normalized_company,
                fiscal_year=fiscal_year,
                offset=offset,
            )
            if expected_total is None:
                expected_total = total_size
                if expected_total > self._configuration.max_records:
                    raise SettlementAdjustmentClientError("DGC_RECORD_LIMIT_EXCEEDED")
            elif total_size != expected_total:
                raise SettlementAdjustmentClientError("DGC_TOTAL_SIZE_CHANGED")
            rows.extend(page_rows)
            if len(rows) > self._configuration.max_records:
                raise SettlementAdjustmentClientError("DGC_RECORD_LIMIT_EXCEEDED")
            if (
                not page_rows
                or len(rows) >= total_size
                or len(page_rows) < self._configuration.page_size
            ):
                return tuple(rows)
            offset += len(page_rows)
        raise SettlementAdjustmentClientError("DGC_PAGE_LIMIT_EXCEEDED")

    def _fetch_page(
        self,
        *,
        company: str,
        fiscal_year: str,
        offset: int,
    ) -> tuple[tuple[SettlementAdjustmentRow, ...], int]:
        body = json.dumps(
            {
                "company": company,
                "fiscal_year": fiscal_year,
                "offsetValue": offset,
                "limitValue": self._configuration.page_size,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("DGC signing clock must return a timezone-aware datetime")
        sdk_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = build_apig_headers(
            endpoint=self._configuration.endpoint,
            app_key=self._configuration.app_key.get_secret_value(),
            app_secret=self._configuration.app_secret.get_secret_value(),
            body=body,
            sdk_date=sdk_date,
        )
        response = self._client.post(
            self._configuration.endpoint,
            content=body,
            headers=headers,
        )
        request_id = response.headers.get("x-request-id")
        if len(response.content) > self._configuration.max_page_bytes:
            raise SettlementAdjustmentClientError(
                "DGC_PAGE_BYTES_EXCEEDED",
                request_id=request_id,
            )
        try:
            payload = json.loads(response.content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SettlementAdjustmentClientError(
                "DGC_RESPONSE_INVALID_JSON",
                request_id=request_id,
            ) from error
        if not isinstance(payload, dict):
            raise SettlementAdjustmentClientError(
                "DGC_RESPONSE_INVALID_SHAPE",
                request_id=request_id,
            )
        if payload.get("errCode") != "DLM.0":
            raise SettlementAdjustmentClientError(
                str(payload.get("errCode") or f"HTTP_{response.status_code}"),
                request_id=request_id,
            )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise SettlementAdjustmentClientError(
                "DGC_RESPONSE_NOT_SUCCESSFUL",
                request_id=request_id,
            )
        return _parse_page(
            data,
            company=company,
            fiscal_year=fiscal_year,
            request_id=request_id,
        )


def _parse_page(
    data: dict[str, object],
    *,
    company: str,
    fiscal_year: str,
    request_id: str | None,
) -> tuple[tuple[SettlementAdjustmentRow, ...], int]:
    raw_columns = data.get("columnNames")
    raw_rows = data.get("data")
    total_size = _nonnegative_int(
        data.get("totalSize"),
        "DGC_TOTAL_SIZE_INVALID",
        request_id,
    )
    row_size = _nonnegative_int(
        data.get("rowSize"),
        "DGC_ROW_SIZE_INVALID",
        request_id,
    )
    if not isinstance(raw_columns, list) or not all(isinstance(item, str) for item in raw_columns):
        raise SettlementAdjustmentClientError("DGC_COLUMNS_INVALID", request_id=request_id)
    columns = tuple(raw_columns)
    is_scoped_response = set(columns) == set(SCOPED_FIELDS)
    is_full_response = set(columns) == set(FULL_FIELDS)
    if not is_scoped_response and not is_full_response:
        raise SettlementAdjustmentClientError("DGC_SCHEMA_DRIFT", request_id=request_id)
    if not isinstance(raw_rows, list) or row_size != len(raw_rows):
        raise SettlementAdjustmentClientError(
            "DGC_ROW_COUNT_MISMATCH",
            request_id=request_id,
        )

    parsed_rows: list[SettlementAdjustmentRow] = []
    expected_fields = set(columns)
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict) or set(raw_row) != expected_fields:
            raise SettlementAdjustmentClientError(
                "DGC_ROW_SCHEMA_DRIFT",
                request_id=request_id,
            )
        values = dict(raw_row)
        if is_scoped_response:
            values["company"] = company
            values["companyname"] = None
        elif str(values.get("company") or "").strip() != company:
            raise SettlementAdjustmentClientError(
                "DGC_COMPANY_SCOPE_MISMATCH",
                request_id=request_id,
            )
        try:
            row = SettlementAdjustmentRow.model_validate(values)
        except ValueError as error:
            raise SettlementAdjustmentClientError(
                "DGC_ROW_VALIDATION_FAILED",
                request_id=request_id,
            ) from error
        if row.fiscal_year != fiscal_year:
            raise SettlementAdjustmentClientError(
                "DGC_YEAR_SCOPE_MISMATCH",
                request_id=request_id,
            )
        parsed_rows.append(row)
    return tuple(parsed_rows), total_size


def _nonnegative_int(value: object, error_code: str, request_id: str | None) -> int:
    if isinstance(value, bool):
        raise SettlementAdjustmentClientError(error_code, request_id=request_id)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as error:
            raise SettlementAdjustmentClientError(error_code, request_id=request_id) from error
    else:
        raise SettlementAdjustmentClientError(error_code, request_id=request_id)
    if parsed < 0:
        raise SettlementAdjustmentClientError(error_code, request_id=request_id)
    return parsed


__all__ = [
    "FULL_FIELDS",
    "SCOPED_FIELDS",
    "SettlementAdjustmentClient",
    "SettlementAdjustmentClientConfiguration",
    "SettlementAdjustmentClientError",
    "build_apig_headers",
]

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import json
import ssl
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from tax_risk.adapters.dgc.settlement_adjustment import build_apig_headers
from tax_risk.application.tax_adjustment_accounts.contracts import SapIncomeRow


SAP_INCOME_FIELDS = (
    "mandt",
    "bukrs",
    "companyname",
    "gjahr",
    "monat",
    "rldnr",
    "hs",
    "ztext",
    "nmhsl",
    "nyhsl",
)


class SapIncomeClientConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    endpoint: str
    app_key: SecretStr
    app_secret: SecretStr
    page_size: int = Field(default=15_000, gt=0, le=15_000)
    max_records: int = Field(default=500_000, gt=0)
    max_pages: int = Field(default=100, gt=0)
    max_page_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=240, gt=0, le=600)

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_plain_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("sapincome endpoint must use HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("sapincome endpoint cannot contain query or fragment")
        return value.rstrip("/")


class SapIncomeClientError(RuntimeError):
    def __init__(self, error_code: str, *, request_id: str | None = None) -> None:
        self.error_code = error_code
        self.request_id = request_id
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"sapincome failed: {error_code}{suffix}")


class SapIncomeClient:
    def __init__(
        self,
        configuration: SapIncomeClientConfiguration,
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

    def __enter__(self) -> SapIncomeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_rows(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
    ) -> tuple[SapIncomeRow, ...]:
        company = company_code.strip()
        if not company:
            raise ValueError("company_code is required")
        if len(fiscal_year) != 4 or not fiscal_year.isdigit():
            raise ValueError("fiscal_year must contain four digits")
        normalized_period = _normalize_period(fiscal_period)

        rows: list[SapIncomeRow] = []
        offset = 0
        expected_total: int | None = None
        for _ in range(self._configuration.max_pages):
            page_rows, total_size = self._fetch_page(
                company_code=company,
                fiscal_year=fiscal_year,
                fiscal_period=normalized_period,
                offset=offset,
            )
            if expected_total is None:
                expected_total = total_size
                if expected_total > self._configuration.max_records:
                    raise SapIncomeClientError("DGC_RECORD_LIMIT_EXCEEDED")
            elif total_size != expected_total:
                raise SapIncomeClientError("DGC_TOTAL_SIZE_CHANGED")
            rows.extend(page_rows)
            if len(rows) > self._configuration.max_records:
                raise SapIncomeClientError("DGC_RECORD_LIMIT_EXCEEDED")
            if len(rows) >= total_size:
                return tuple(rows)
            if not page_rows or len(page_rows) < self._configuration.page_size:
                raise SapIncomeClientError("DGC_PREMATURE_PAGE_END")
            offset += len(page_rows)
        raise SapIncomeClientError("DGC_PAGE_LIMIT_EXCEEDED")

    def _fetch_page(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
        offset: int,
    ) -> tuple[tuple[SapIncomeRow, ...], int]:
        body = json.dumps(
            {
                "bukrs": company_code,
                "gjahr": fiscal_year,
                "monat": fiscal_period,
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
            raise SapIncomeClientError(
                "DGC_PAGE_BYTES_EXCEEDED",
                request_id=request_id,
            )
        try:
            payload = json.loads(response.content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SapIncomeClientError(
                "DGC_RESPONSE_INVALID_JSON",
                request_id=request_id,
            ) from error
        if not isinstance(payload, dict):
            raise SapIncomeClientError(
                "DGC_RESPONSE_INVALID_SHAPE",
                request_id=request_id,
            )
        if payload.get("errCode") != "DLM.0":
            raise SapIncomeClientError(
                str(payload.get("errCode") or f"HTTP_{response.status_code}"),
                request_id=request_id,
            )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise SapIncomeClientError(
                "DGC_RESPONSE_NOT_SUCCESSFUL",
                request_id=request_id,
            )
        return _parse_page(
            data,
            company_code=company_code,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            request_id=request_id,
        )


def _parse_page(
    data: dict[str, object],
    *,
    company_code: str,
    fiscal_year: str,
    fiscal_period: str,
    request_id: str | None,
) -> tuple[tuple[SapIncomeRow, ...], int]:
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
    if not isinstance(raw_columns, list) or tuple(raw_columns) != SAP_INCOME_FIELDS:
        raise SapIncomeClientError("DGC_SCHEMA_DRIFT", request_id=request_id)
    if not isinstance(raw_rows, list) or row_size != len(raw_rows):
        raise SapIncomeClientError(
            "DGC_ROW_COUNT_MISMATCH",
            request_id=request_id,
        )

    parsed_rows: list[SapIncomeRow] = []
    expected_fields = set(SAP_INCOME_FIELDS)
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict) or set(raw_row) != expected_fields:
            raise SapIncomeClientError(
                "DGC_ROW_SCHEMA_DRIFT",
                request_id=request_id,
            )
        try:
            row = SapIncomeRow.model_validate(raw_row)
        except ValueError as error:
            raise SapIncomeClientError(
                "DGC_ROW_VALIDATION_FAILED",
                request_id=request_id,
            ) from error
        if row.bukrs != company_code:
            raise SapIncomeClientError(
                "DGC_COMPANY_SCOPE_MISMATCH",
                request_id=request_id,
            )
        if row.gjahr != fiscal_year:
            raise SapIncomeClientError(
                "DGC_YEAR_SCOPE_MISMATCH",
                request_id=request_id,
            )
        if row.monat != fiscal_period:
            raise SapIncomeClientError(
                "DGC_PERIOD_SCOPE_MISMATCH",
                request_id=request_id,
            )
        parsed_rows.append(row)
    return tuple(parsed_rows), total_size


def _normalize_period(value: str) -> str:
    normalized = value.strip()
    if not normalized.isdigit():
        raise ValueError("fiscal_period must contain digits")
    month = int(normalized)
    if not 1 <= month <= 12:
        raise ValueError("fiscal_period must be between 1 and 12")
    return f"{month:02d}"


def _nonnegative_int(value: object, error_code: str, request_id: str | None) -> int:
    if isinstance(value, bool):
        raise SapIncomeClientError(error_code, request_id=request_id)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as error:
            raise SapIncomeClientError(error_code, request_id=request_id) from error
    else:
        raise SapIncomeClientError(error_code, request_id=request_id)
    if parsed < 0:
        raise SapIncomeClientError(error_code, request_id=request_id)
    return parsed


__all__ = [
    "SAP_INCOME_FIELDS",
    "SapIncomeClient",
    "SapIncomeClientConfiguration",
    "SapIncomeClientError",
]

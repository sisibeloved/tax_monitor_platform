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
from tax_risk.application.tax_adjustment_accounts.contracts import TrialBalanceRow


TRIAL_BALANCE_FIELDS = (
    "company_code",
    "company_name",
    "fiscal_year",
    "fiscal_period",
    "gl_account_code",
    "gl_account_name",
    "bank_center_code",
    "bank_account_number",
    "cost_center_code",
    "cost_center_name",
    "profit_center_code",
    "profit_center_name",
    "internal_order_code",
    "internal_order_name",
    "business_area_code",
    "business_area_name",
    "customer_code",
    "customer_name",
    "vendor_code",
    "vendor_name",
    "asset_code",
    "asset_name",
    "rstgr",
    "rstgr_name",
    "input_tax_process_method",
    "sfkf",
    "total_debit_amount",
    "total_credit_amount",
)


class TrialBalanceClientConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    endpoint: str
    app_key: SecretStr
    app_secret: SecretStr
    page_size: int = Field(default=15_000, gt=0, le=15_000)
    max_records: int = Field(default=500_000, gt=0)
    max_pages: int = Field(default=100, gt=0)
    max_page_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=240, gt=0, le=600)

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_plain_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("trial_balance endpoint must use HTTPS")
        if parsed.query or parsed.fragment:
            raise ValueError("trial_balance endpoint cannot contain query or fragment")
        return value.rstrip("/")


class TrialBalanceClientError(RuntimeError):
    def __init__(self, error_code: str, *, request_id: str | None = None) -> None:
        self.error_code = error_code
        self.request_id = request_id
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"trial_balance failed: {error_code}{suffix}")


class TrialBalanceClient:
    def __init__(
        self,
        configuration: TrialBalanceClientConfiguration,
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

    def __enter__(self) -> TrialBalanceClient:
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
    ) -> tuple[TrialBalanceRow, ...]:
        company = company_code.strip()
        if not company:
            raise ValueError("company_code is required")
        if len(fiscal_year) != 4 or not fiscal_year.isdigit():
            raise ValueError("fiscal_year must contain four digits")
        if (
            len(fiscal_period) != 3
            or not fiscal_period.isdigit()
            or not 1 <= int(fiscal_period) <= 12
        ):
            raise ValueError("fiscal_period must be between 001 and 012")

        rows: list[TrialBalanceRow] = []
        offset = 0
        for _ in range(self._configuration.max_pages):
            page_rows = self._fetch_page(
                company_code=company,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                offset=offset,
            )
            rows.extend(page_rows)
            if len(rows) > self._configuration.max_records:
                raise TrialBalanceClientError("DGC_RECORD_LIMIT_EXCEEDED")
            if len(page_rows) < self._configuration.page_size:
                return tuple(rows)
            offset += len(page_rows)
        raise TrialBalanceClientError("DGC_PAGE_LIMIT_EXCEEDED")

    def _fetch_page(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
        offset: int,
    ) -> tuple[TrialBalanceRow, ...]:
        body = json.dumps(
            {
                "company_code": company_code,
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
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
            raise TrialBalanceClientError(
                "DGC_PAGE_BYTES_EXCEEDED",
                request_id=request_id,
            )
        try:
            payload = json.loads(response.content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrialBalanceClientError(
                "DGC_RESPONSE_INVALID_JSON",
                request_id=request_id,
            ) from error
        if not isinstance(payload, dict):
            raise TrialBalanceClientError(
                "DGC_RESPONSE_INVALID_SHAPE",
                request_id=request_id,
            )
        if payload.get("errCode") != "DLM.0":
            raise TrialBalanceClientError(
                str(payload.get("errCode") or f"HTTP_{response.status_code}"),
                request_id=request_id,
            )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise TrialBalanceClientError(
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
) -> tuple[TrialBalanceRow, ...]:
    raw_columns = data.get("columnNames")
    raw_rows = data.get("data")
    row_size = _nonnegative_int(
        data.get("rowSize"),
        "DGC_ROW_SIZE_INVALID",
        request_id,
    )
    if not isinstance(raw_columns, list) or tuple(raw_columns) != TRIAL_BALANCE_FIELDS:
        raise TrialBalanceClientError("DGC_SCHEMA_DRIFT", request_id=request_id)
    if not isinstance(raw_rows, list) or row_size != len(raw_rows):
        raise TrialBalanceClientError(
            "DGC_ROW_COUNT_MISMATCH",
            request_id=request_id,
        )

    parsed_rows: list[TrialBalanceRow] = []
    expected_fields = set(TRIAL_BALANCE_FIELDS)
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict) or set(raw_row) != expected_fields:
            raise TrialBalanceClientError(
                "DGC_ROW_SCHEMA_DRIFT",
                request_id=request_id,
            )
        try:
            row = TrialBalanceRow.model_validate(raw_row)
        except ValueError as error:
            raise TrialBalanceClientError(
                "DGC_ROW_VALIDATION_FAILED",
                request_id=request_id,
            ) from error
        if row.company_code != company_code:
            raise TrialBalanceClientError(
                "DGC_COMPANY_SCOPE_MISMATCH",
                request_id=request_id,
            )
        if row.fiscal_year != fiscal_year:
            raise TrialBalanceClientError(
                "DGC_YEAR_SCOPE_MISMATCH",
                request_id=request_id,
            )
        if row.fiscal_period != fiscal_period:
            raise TrialBalanceClientError(
                "DGC_PERIOD_SCOPE_MISMATCH",
                request_id=request_id,
            )
        parsed_rows.append(row)
    return tuple(parsed_rows)


def _nonnegative_int(value: object, error_code: str, request_id: str | None) -> int:
    if isinstance(value, bool):
        raise TrialBalanceClientError(error_code, request_id=request_id)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as error:
            raise TrialBalanceClientError(error_code, request_id=request_id) from error
    else:
        raise TrialBalanceClientError(error_code, request_id=request_id)
    if parsed < 0:
        raise TrialBalanceClientError(error_code, request_id=request_id)
    return parsed


__all__ = [
    "TRIAL_BALANCE_FIELDS",
    "TrialBalanceClient",
    "TrialBalanceClientConfiguration",
    "TrialBalanceClientError",
]

from __future__ import annotations

from collections.abc import Iterator, Mapping
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import re
from typing import Literal, Never, cast

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    CanonicalRowValidationError,
    CompanyMasterRow,
    RowError,
)


FINANCIAL_HEADER = (
    "source_record_key",
    "company_code",
    "fiscal_year",
    "period",
    "currency",
    "amount_scale",
    "metric_code",
    "amount",
    "extracted_at",
)
COMPANY_MASTER_HEADER = (
    "source_record_key",
    "company_code",
    "company_name",
    "lifecycle",
    "extracted_at",
)
_HEADERS = {
    "company_master": COMPANY_MASTER_HEADER,
    "quarterly_metric": FINANCIAL_HEADER,
    "WELFARE_YTD": FINANCIAL_HEADER,
    "SALARY_YTD": FINANCIAL_HEADER,
    "DONATION_YTD": FINANCIAL_HEADER,
    "PROFIT_YTD": FINANCIAL_HEADER,
}
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")


class HeaderValidationError(ValueError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        missing_columns: tuple[str, ...] = (),
        extra_columns: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.missing_columns = missing_columns
        self.extra_columns = extra_columns


@dataclass(frozen=True, slots=True)
class _FieldError(Exception):
    error_code: str
    message: str
    field: str
    rejected_value: str | None


class CSVAdapter:
    """Read-only reference adapter for controlled UTF-8 CSV bulk files."""

    def __init__(self, payload: bytes, *, dataset_code: str) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("CSV payload must be bytes")
        self._payload = payload
        self._dataset_code = dataset_code
        self._raw: bytes | None = None
        self._text: str | None = None
        self._checksum: str | None = None

    @property
    def checksum(self) -> str:
        self._read_raw_and_hash()
        assert self._checksum is not None
        return self._checksum

    def validate_header(self) -> None:
        expected = _HEADERS.get(self._dataset_code)
        if expected is None:
            raise HeaderValidationError(
                "UNSUPPORTED_DATASET",
                f"CSV adapter does not support dataset_code {self._dataset_code!r}",
            )

        reader = csv.DictReader(io.StringIO(self._read_and_hash(), newline=""))
        actual = tuple(reader.fieldnames or ())
        missing = tuple(column for column in expected if column not in actual)
        extra = tuple(column for column in actual if column not in expected)
        duplicates = len(set(actual)) != len(actual)
        if missing or extra or duplicates:
            message = (
                f"invalid header for {self._dataset_code}: expected {expected!r}, "
                f"received {actual!r}"
            )
            raise HeaderValidationError(
                "INVALID_HEADER",
                message,
                missing_columns=missing,
                extra_columns=extra,
            )

    def iter_rows(self) -> Iterator[AdapterRow]:
        self.validate_header()
        reader = csv.DictReader(io.StringIO(self._read_and_hash(), newline=""))
        for raw in reader:
            row_number = reader.line_num
            if None in raw or any(value is None for key, value in raw.items() if key is not None):
                yield AdapterRow(
                    row_number=row_number,
                    value=None,
                    error=RowError(
                        row_number=row_number,
                        error_code="ROW_COLUMN_COUNT_MISMATCH",
                        message="row column count does not match the CSV header",
                        context=_safe_error_context(raw),
                    ),
                )
                continue
            try:
                value = (
                    self._parse_company_master(raw)
                    if self._dataset_code == "company_master"
                    else self._parse_financial(raw)
                )
            except (_FieldError, CanonicalRowValidationError) as error:
                yield AdapterRow(
                    row_number=row_number,
                    value=None,
                    error=RowError(
                        row_number=row_number,
                        error_code=error.error_code,
                        message=error.message,
                        field=error.field,
                        rejected_value=error.rejected_value,
                        context=_safe_error_context(raw),
                    ),
                )
            else:
                yield AdapterRow(row_number=row_number, value=value, error=None)

    def _read_and_hash(self) -> str:
        if self._text is not None:
            return self._text
        raw = self._read_raw_and_hash()
        try:
            self._text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HeaderValidationError(
                "INVALID_ENCODING",
                "CSV file must be UTF-8 encoded",
            ) from error
        return self._text

    def _read_raw_and_hash(self) -> bytes:
        if self._raw is not None:
            return self._raw
        digest = sha256()
        payload_view = memoryview(self._payload)
        for offset in range(0, len(payload_view), 64 * 1024):
            digest.update(payload_view[offset : offset + 64 * 1024])
        self._raw = self._payload
        self._checksum = digest.hexdigest()
        return self._raw

    def _parse_financial(
        self,
        raw: Mapping[str | None, str | list[str] | None],
    ) -> CanonicalFinancialRow:
        source_record_key = _required(raw, "source_record_key")
        company_code = _required(raw, "company_code")
        fiscal_year = _integer(raw, "fiscal_year", minimum=2000, maximum=9999)
        period = _date(raw, "period")
        if fiscal_year != period.year:
            _fail(
                "FISCAL_YEAR_MISMATCH",
                "fiscal_year must match period year",
                "fiscal_year",
                str(fiscal_year),
            )
        currency = _required(raw, "currency").upper()
        if _CURRENCY_PATTERN.fullmatch(currency) is None:
            _fail("INVALID_CURRENCY", "currency must be three letters", "currency", currency)
        amount_scale = _integer(raw, "amount_scale", minimum=0, maximum=12)
        metric_code = _required(raw, "metric_code")
        amount_text = _required(raw, "amount")
        try:
            amount = Decimal(amount_text)
        except InvalidOperation:
            _fail(
                "INVALID_DECIMAL",
                "amount must be a finite decimal string",
                "amount",
                amount_text,
            )
        if not amount.is_finite():
            _fail(
                "INVALID_DECIMAL",
                "amount must be a finite decimal string",
                "amount",
                amount_text,
            )
        exponent = cast(int, amount.as_tuple().exponent)
        fractional_digits = max(-exponent, 0)
        if fractional_digits > amount_scale:
            _fail(
                "AMOUNT_SCALE_MISMATCH",
                "amount has more fractional digits than amount_scale",
                "amount",
                amount_text,
            )
        extracted_at = _datetime(raw, "extracted_at")
        return CanonicalFinancialRow(
            source_record_key=source_record_key,
            company_code=company_code,
            fiscal_year=fiscal_year,
            period=period,
            currency=currency,
            amount_scale=amount_scale,
            metric_code=metric_code,
            amount=amount,
            extracted_at=extracted_at,
        )

    def _parse_company_master(
        self,
        raw: Mapping[str | None, str | list[str] | None],
    ) -> CompanyMasterRow:
        lifecycle = _required(raw, "lifecycle").upper()
        if lifecycle not in {"ACTIVE", "INACTIVE"}:
            _fail(
                "INVALID_LIFECYCLE",
                "lifecycle must be ACTIVE or INACTIVE",
                "lifecycle",
                lifecycle,
            )
        return CompanyMasterRow(
            source_record_key=_required(raw, "source_record_key"),
            company_code=_required(raw, "company_code"),
            company_name=_required(raw, "company_name"),
            lifecycle=cast(Literal["ACTIVE", "INACTIVE"], lifecycle),
            extracted_at=_datetime(raw, "extracted_at"),
        )


def _raw_value(
    raw: Mapping[str | None, str | list[str] | None],
    field: str,
) -> str | None:
    value = raw.get(field)
    if isinstance(value, str):
        return value.strip()
    return None


def _safe_error_context(
    raw: Mapping[str | None, str | list[str] | None],
) -> tuple[tuple[str, str], ...]:
    context: list[tuple[str, str]] = []
    for field, maximum_length in (("company_code", 64), ("metric_code", 128)):
        value = _raw_value(raw, field)
        if value and len(value) <= maximum_length:
            context.append((field, value))
    return tuple(context)


def _required(raw: Mapping[str | None, str | list[str] | None], field: str) -> str:
    value = _raw_value(raw, field)
    if value is None or value == "":
        _fail("MISSING_VALUE", f"{field} is required", field, value)
    return value


def _integer(
    raw: Mapping[str | None, str | list[str] | None],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = _required(raw, field)
    try:
        parsed = int(value)
    except ValueError:
        _fail("INVALID_INTEGER", f"{field} must be an integer", field, value)
    if str(parsed) != value and f"+{parsed}" != value:
        _fail("INVALID_INTEGER", f"{field} must be an integer", field, value)
    if parsed < minimum or parsed > maximum:
        _fail(
            "INTEGER_OUT_OF_RANGE",
            f"{field} must be between {minimum} and {maximum}",
            field,
            value,
        )
    return parsed


def _date(raw: Mapping[str | None, str | list[str] | None], field: str) -> date:
    value = _required(raw, field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail("INVALID_DATE", f"{field} must be an ISO date", field, value)


def _datetime(raw: Mapping[str | None, str | list[str] | None], field: str) -> datetime:
    value = _required(raw, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("INVALID_DATETIME", f"{field} must be an ISO datetime", field, value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(
            "TIMEZONE_REQUIRED",
            f"{field} must include a UTC offset",
            field,
            value,
        )
    return parsed


def _fail(error_code: str, message: str, field: str, value: str | None) -> Never:
    raise _FieldError(error_code, message, field, value)


__all__ = ["CSVAdapter", "HeaderValidationError"]

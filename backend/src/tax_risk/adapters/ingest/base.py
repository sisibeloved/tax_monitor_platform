from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Literal, Protocol, cast


_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")


class CanonicalRowValidationError(ValueError):
    def __init__(
        self,
        error_code: str,
        message: str,
        field: str,
        rejected_value: str | None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.field = field
        self.rejected_value = rejected_value


@dataclass(frozen=True, slots=True)
class CanonicalFinancialRow:
    source_record_key: str
    company_code: str
    fiscal_year: int
    period: date
    currency: str
    amount_scale: int
    metric_code: str
    amount: Decimal
    extracted_at: datetime

    def __post_init__(self) -> None:
        _validate_text(self.source_record_key, "source_record_key", maximum_length=512)
        _validate_text(self.company_code, "company_code", maximum_length=64)
        if type(self.fiscal_year) is not int:
            raise TypeError("fiscal_year must be an integer")
        if not 2000 <= self.fiscal_year <= 9999:
            _invalid(
                "INTEGER_OUT_OF_RANGE",
                "fiscal_year must be between 2000 and 9999",
                "fiscal_year",
                str(self.fiscal_year),
            )
        if type(self.period) is not date:
            raise TypeError("period must be a date")
        if self.fiscal_year != self.period.year:
            _invalid(
                "FISCAL_YEAR_MISMATCH",
                "fiscal_year must match period year",
                "fiscal_year",
                str(self.fiscal_year),
            )
        if not isinstance(self.currency, str):
            raise TypeError("currency must be a string")
        if _CURRENCY_PATTERN.fullmatch(self.currency) is None:
            _invalid(
                "INVALID_CURRENCY",
                "currency must be three uppercase letters",
                "currency",
                self.currency,
            )
        if type(self.amount_scale) is not int:
            raise TypeError("amount_scale must be an integer")
        if not 0 <= self.amount_scale <= 12:
            _invalid(
                "INTEGER_OUT_OF_RANGE",
                "amount_scale must be between 0 and 12",
                "amount_scale",
                str(self.amount_scale),
            )
        _validate_text(self.metric_code, "metric_code", maximum_length=128)
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be Decimal parsed from source text")
        if not self.amount.is_finite():
            _invalid(
                "INVALID_DECIMAL",
                "amount must be finite",
                "amount",
                str(self.amount),
            )
        exponent = cast(int, self.amount.as_tuple().exponent)
        if max(-exponent, 0) > self.amount_scale:
            _invalid(
                "AMOUNT_SCALE_MISMATCH",
                "amount has more fractional digits than amount_scale",
                "amount",
                str(self.amount),
            )
        if not fits_database_amount(self.amount):
            _invalid(
                "DECIMAL_OUT_OF_RANGE",
                "amount exceeds NUMERIC(38, 12)",
                "amount",
                str(self.amount),
            )
        _validate_aware_datetime(self.extracted_at, "extracted_at")


@dataclass(frozen=True, slots=True)
class CompanyMasterRow:
    source_record_key: str
    company_code: str
    company_name: str
    lifecycle: Literal["ACTIVE", "INACTIVE"]
    extracted_at: datetime

    def __post_init__(self) -> None:
        _validate_text(self.source_record_key, "source_record_key", maximum_length=512)
        _validate_text(self.company_code, "company_code", maximum_length=64)
        _validate_text(self.company_name, "company_name", maximum_length=256)
        if self.lifecycle not in {"ACTIVE", "INACTIVE"}:
            _invalid(
                "INVALID_LIFECYCLE",
                "lifecycle must be ACTIVE or INACTIVE",
                "lifecycle",
                str(self.lifecycle),
            )
        _validate_aware_datetime(self.extracted_at, "extracted_at")


CanonicalRow = CanonicalFinancialRow | CompanyMasterRow


@dataclass(frozen=True, slots=True)
class RowError:
    row_number: int
    error_code: str
    message: str
    field: str | None = None
    rejected_value: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterRow:
    row_number: int
    value: CanonicalRow | None
    error: RowError | None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.error is None):
            raise ValueError("adapter row must contain exactly one of value or error")


class BulkFileAdapter(Protocol):
    @property
    def checksum(self) -> str: ...

    def validate_header(self) -> None: ...

    def iter_rows(self) -> Iterator[AdapterRow]: ...


def fits_database_amount(amount: Decimal) -> bool:
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return False
    integral_digits = max(amount.adjusted() + 1, 0) if amount else 0
    exponent = cast(int, amount.as_tuple().exponent)
    fractional_digits = max(-exponent, 0)
    return integral_digits <= 26 and fractional_digits <= 12


def _validate_text(value: object, field: str, *, maximum_length: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip():
        _invalid("MISSING_VALUE", f"{field} is required", field, value)
    if len(value) > maximum_length:
        _invalid(
            "VALUE_TOO_LONG",
            f"{field} must not exceed {maximum_length} characters",
            field,
            value,
        )


def _validate_aware_datetime(value: object, field: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(
            "TIMEZONE_REQUIRED",
            f"{field} must include a UTC offset",
            field,
            value.isoformat(),
        )


def _invalid(
    error_code: str,
    message: str,
    field: str,
    rejected_value: str | None,
) -> None:
    raise CanonicalRowValidationError(
        error_code,
        message,
        field,
        rejected_value,
    )


__all__ = [
    "AdapterRow",
    "BulkFileAdapter",
    "CanonicalFinancialRow",
    "CanonicalRow",
    "CanonicalRowValidationError",
    "CompanyMasterRow",
    "RowError",
    "fits_database_amount",
]

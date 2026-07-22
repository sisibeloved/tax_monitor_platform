"""Strict quarterly metric adapter for the DGC SAP 科目发生额 API."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from typing import Final, cast

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    fits_database_amount,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


DGC_SAP_TRIAL_BALANCE_TABLE_NAME: Final[str] = "SAP科目发生额"
CURRENT_INCOME_TAX_GL_ACCOUNT: Final[str] = "6801010000"
PRIOR_QUARTER_CURRENT_TAX_METRIC: Final[str] = "prior_quarter_current_tax"
CURRENT_QUARTER_CURRENT_TAX_METRIC: Final[str] = "current_quarter_current_tax"
DGC_SAP_TRIAL_BALANCE_FIELDS: Final[tuple[str, ...]] = (
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


class DgcSapTrialBalanceError(ValueError):
    """Stable failure for an unsafe SAP trial-balance response."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        row_number: int | None = None,
        field: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.row_number = row_number
        self.field = field
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DgcSapTrialBalanceRecord:
    company_code: str
    fiscal_year: int
    fiscal_period: int
    gl_account_code: str
    total_debit_amount: Decimal
    total_credit_amount: Decimal


@dataclass(frozen=True, slots=True)
class DgcSapTrialBalanceResult:
    prior_quarter_current_tax: Decimal
    current_quarter_current_tax: Decimal
    source_row_count: int
    rows_through_period: int
    source_checksum: str


class DgcSapTrialBalanceAdapter:
    """Aggregate account 6801010000 by quarters without changing source signs."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company_code: str,
        expected_fiscal_year: int | str,
        through_period: int,
    ) -> None:
        if not isinstance(result, DgcFetchResult):
            raise TypeError("result must be a DgcFetchResult")
        self._result = result
        self._expected_company_code = _expected_company(expected_company_code)
        self._expected_fiscal_year = _expected_year(expected_fiscal_year)
        self._through_period = _quarter_end_period(through_period)

    def adapt(self) -> DgcSapTrialBalanceResult:
        if not self._result.records:
            return DgcSapTrialBalanceResult(
                prior_quarter_current_tax=Decimal(0),
                current_quarter_current_tax=Decimal(0),
                source_row_count=0,
                rows_through_period=0,
                source_checksum=self._result.checksum,
            )
        records = tuple(
            self._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        through_records = tuple(
            record for record in records if record.fiscal_period <= self._through_period
        )
        if not through_records:
            raise DgcSapTrialBalanceError(
                "NO_ROWS_THROUGH_PERIOD",
                "SAP trial-balance response did not contain rows through the requested quarter",
                field="fiscal_period",
            )

        quarter_start = self._through_period - 2
        prior_amounts = tuple(
            amount
            for record in through_records
            if record.fiscal_period < quarter_start
            for amount in (record.total_debit_amount, record.total_credit_amount)
        )
        current_amounts = tuple(
            amount
            for record in through_records
            if quarter_start <= record.fiscal_period <= self._through_period
            for amount in (record.total_debit_amount, record.total_credit_amount)
        )
        prior_total = _exact_sum(prior_amounts)
        current_total = _exact_sum(current_amounts)
        for metric_code, amount in (
            (PRIOR_QUARTER_CURRENT_TAX_METRIC, prior_total),
            (CURRENT_QUARTER_CURRENT_TAX_METRIC, current_total),
        ):
            if not fits_database_amount(amount):
                raise DgcSapTrialBalanceError(
                    "AGGREGATE_AMOUNT_OUT_OF_RANGE",
                    f"{metric_code} must fit NUMERIC(38,12)",
                    field=metric_code,
                )
        return DgcSapTrialBalanceResult(
            prior_quarter_current_tax=prior_total,
            current_quarter_current_tax=current_total,
            source_row_count=len(records),
            rows_through_period=len(through_records),
            source_checksum=self._result.checksum,
        )

    def _parse_record(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> DgcSapTrialBalanceRecord:
        if any(not isinstance(key, str) for key in raw):
            raise DgcSapTrialBalanceError(
                "INVALID_RESPONSE_FIELD",
                "SAP trial-balance response field names must be strings",
                row_number=row_number,
            )
        actual_fields = set(raw)
        expected_fields = set(DGC_SAP_TRIAL_BALANCE_FIELDS)
        unexpected = tuple(sorted(actual_fields - expected_fields))
        if unexpected:
            raise DgcSapTrialBalanceError(
                "UNEXPECTED_RESPONSE_FIELD",
                f"SAP trial-balance row contains unexpected fields: {', '.join(unexpected)}",
                row_number=row_number,
                field=unexpected[0],
            )
        missing = tuple(sorted(expected_fields - actual_fields))
        if missing:
            raise DgcSapTrialBalanceError(
                "MISSING_RESPONSE_FIELD",
                f"SAP trial-balance row is missing fields: {', '.join(missing)}",
                row_number=row_number,
                field=missing[0],
            )

        company_code = _text(raw["company_code"], "company_code", row_number)
        fiscal_year = _year(raw["fiscal_year"], row_number)
        fiscal_period = _period(raw["fiscal_period"], row_number)
        gl_account_code = _text(raw["gl_account_code"], "gl_account_code", row_number)
        if company_code != self._expected_company_code:
            raise DgcSapTrialBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "SAP trial-balance response contained a company outside the requested scope",
                row_number=row_number,
                field="company_code",
            )
        if fiscal_year != self._expected_fiscal_year:
            raise DgcSapTrialBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "SAP trial-balance response contained a fiscal year outside the requested scope",
                row_number=row_number,
                field="fiscal_year",
            )
        if gl_account_code != CURRENT_INCOME_TAX_GL_ACCOUNT:
            raise DgcSapTrialBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "SAP trial-balance response contained an account outside the requested scope",
                row_number=row_number,
                field="gl_account_code",
            )
        return DgcSapTrialBalanceRecord(
            company_code=company_code,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            gl_account_code=gl_account_code,
            total_debit_amount=_amount(raw["total_debit_amount"], "total_debit_amount", row_number),
            total_credit_amount=_amount(
                raw["total_credit_amount"],
                "total_credit_amount",
                row_number,
            ),
        )


class DgcSapTrialBalanceMetricAdapter:
    """Materialize the two quarter-specific tax-expense metrics."""

    def __init__(
        self,
        result: DgcSapTrialBalanceResult,
        *,
        company_code: str,
        fiscal_year: int,
        through_period: int,
        currency: str,
        amount_scale: int,
        extracted_at: datetime,
    ) -> None:
        if not isinstance(result, DgcSapTrialBalanceResult):
            raise TypeError("result must be a DgcSapTrialBalanceResult")
        normalized_company = _expected_company(company_code)
        normalized_year = _expected_year(fiscal_year)
        normalized_period = _quarter_end_period(through_period)
        normalized_currency = _currency(currency)
        period = date(
            normalized_year,
            normalized_period,
            monthrange(normalized_year, normalized_period)[1],
        )
        rows: list[CanonicalFinancialRow] = []
        for metric_code, amount in (
            (PRIOR_QUARTER_CURRENT_TAX_METRIC, result.prior_quarter_current_tax),
            (CURRENT_QUARTER_CURRENT_TAX_METRIC, result.current_quarter_current_tax),
        ):
            identity = json.dumps(
                (
                    normalized_company,
                    str(normalized_year),
                    f"{normalized_period:02d}",
                    CURRENT_INCOME_TAX_GL_ACCOUNT,
                    metric_code,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            rows.append(
                CanonicalFinancialRow(
                    source_record_key=(
                        f"dgc-sap-trial-balance:{sha256(identity).hexdigest()}"
                    ),
                    company_code=normalized_company,
                    fiscal_year=normalized_year,
                    period=period,
                    currency=normalized_currency,
                    amount_scale=amount_scale,
                    metric_code=metric_code,
                    amount=amount,
                    extracted_at=extracted_at,
                )
            )
        self._checksum = result.source_checksum
        self._rows = tuple(rows)

    @property
    def checksum(self) -> str:
        return self._checksum

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        for row_number, row in enumerate(self._rows, start=1):
            yield AdapterRow(row_number=row_number, value=row, error=None)


def _expected_company(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected_company_code must be a non-empty string")
    return value.strip()


def _expected_year(value: object) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise ValueError("expected_fiscal_year must be an integer from 2000 to 9999") from error


def _quarter_end_period(value: object) -> int:
    if type(value) is not int or value not in {3, 6, 9, 12}:
        raise ValueError("through_period must be one of 3, 6, 9, or 12")
    return value


def _year(value: object, row_number: int) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_year must be an integer from 2000 to 9999",
            row_number=row_number,
            field="fiscal_year",
        ) from error


def _integer_year(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped.isascii() or not stripped.isdecimal():
            raise ValueError
        parsed = int(stripped)
    else:
        raise TypeError
    if not 2000 <= parsed <= 9999:
        raise ValueError
    return parsed


def _period(value: object, row_number: int) -> int:
    if not isinstance(value, str):
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_period must be a string from 1/01/001 through 12/012",
            row_number=row_number,
            field="fiscal_period",
        )
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= 3
        or not normalized.isascii()
        or not normalized.isdecimal()
        or not 1 <= int(normalized) <= 12
    ):
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_period must be a string from 1/01/001 through 12/012",
            row_number=row_number,
            field="fiscal_period",
        )
    return int(normalized)


def _text(value: object, field: str, row_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be a non-empty string",
            row_number=row_number,
            field=field,
        )
    return value.strip()


def _amount(value: object, field: str, row_number: int) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an exact decimal, integer, or decimal string",
            row_number=row_number,
            field=field,
        )
    try:
        if isinstance(value, Decimal):
            parsed = value
        elif type(value) is int:
            parsed = Decimal(value)
        elif isinstance(value, str) and value.strip():
            parsed = Decimal(value.strip())
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an exact decimal, integer, or decimal string",
            row_number=row_number,
            field=field,
        ) from error
    if not parsed.is_finite() or not fits_database_amount(parsed):
        raise DgcSapTrialBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be finite and fit NUMERIC(38,12)",
            row_number=row_number,
            field=field,
        )
    return parsed


def _currency(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("currency must be a string")
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be three ASCII letters")
    return normalized


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal(0)
    minimum_exponent = min(cast(int, value.as_tuple().exponent) for value in values)
    maximum_adjusted = max(value.adjusted() for value in values)
    aligned_digits = max(1, maximum_adjusted - minimum_exponent + 1)
    with localcontext() as context:
        context.prec = aligned_digits + len(str(len(values))) + 1
        return sum(values, start=Decimal(0))


__all__ = [
    "CURRENT_INCOME_TAX_GL_ACCOUNT",
    "CURRENT_QUARTER_CURRENT_TAX_METRIC",
    "DGC_SAP_TRIAL_BALANCE_FIELDS",
    "DGC_SAP_TRIAL_BALANCE_TABLE_NAME",
    "PRIOR_QUARTER_CURRENT_TAX_METRIC",
    "DgcSapTrialBalanceAdapter",
    "DgcSapTrialBalanceError",
    "DgcSapTrialBalanceMetricAdapter",
    "DgcSapTrialBalanceRecord",
    "DgcSapTrialBalanceResult",
]

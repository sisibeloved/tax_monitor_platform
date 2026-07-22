"""Strict adapter for quarterly metrics sourced from SAP account balances."""

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


DGC_SAP_ACCOUNT_BALANCE_TABLE_NAME: Final[str] = "SAP科目余额表"
DGC_SAP_ACCOUNT_BALANCE_FIELDS: Final[tuple[str, ...]] = (
    "account_code",
    "account_name",
    "closing_balance",
    "company_code",
    "company_name",
    "credit_amount",
    "debit_amount",
    "fiscal_period",
    "fiscal_year",
    "input_tax_process_method",
    "net_amount",
    "opening_balance",
    "sfkf",
)
OTHER_PAYABLES_ACCRUAL_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {
        "2241050100",
        "2241050200",
        "2241050300",
        "2241050400",
        "2241050500",
        "2241050600",
        "2241050700",
        "2241050800",
        "2241050900",
        "2241059900",
    }
)
DEFERRED_TAX_ASSET_GL_ACCOUNT: Final[str] = "1811030000"


class DgcSapAccountBalanceError(ValueError):
    """Stable, row-aware rejection for an unsafe account-balance response."""

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
class DgcSapAccountBalanceRecord:
    account_code: str
    account_name: str
    closing_balance: Decimal
    company_code: str
    company_name: str
    credit_amount: Decimal
    debit_amount: Decimal
    fiscal_period: int
    fiscal_year: int
    input_tax_process_method: str
    net_amount: Decimal
    opening_balance: Decimal
    sfkf: str


@dataclass(frozen=True, slots=True)
class DgcSapAccountBalanceResult:
    records: tuple[DgcSapAccountBalanceRecord, ...]
    other_payables_records: tuple[DgcSapAccountBalanceRecord, ...]
    deferred_tax_records: tuple[DgcSapAccountBalanceRecord, ...]
    other_payables_accrual: Decimal | None
    sap_cumulative_deferred_tax_expense: Decimal
    source_checksum: str


class DgcSapAccountBalanceAdapter:
    """Validate one company-period response and calculate two quarterly metrics."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company_code: str,
        expected_fiscal_year: int | str,
        expected_fiscal_period: int | str,
    ) -> None:
        if not isinstance(result, DgcFetchResult):
            raise TypeError("result must be a DgcFetchResult")
        self._result = result
        self._expected_company_code = _required_text(
            expected_company_code,
            "expected_company_code",
        )
        self._expected_fiscal_year = _expected_year(expected_fiscal_year)
        self._expected_fiscal_period = _expected_period(expected_fiscal_period)

    def adapt(self) -> DgcSapAccountBalanceResult:
        records = tuple(
            self._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        other_payables_records = tuple(
            record
            for record in records
            if record.account_code in OTHER_PAYABLES_ACCRUAL_ACCOUNTS
        )
        deferred_tax_records = tuple(
            record
            for record in records
            if record.account_code == DEFERRED_TAX_ASSET_GL_ACCOUNT
        )
        other_payables_accrual = (
            _exact_sum(
                tuple(
                    -record.closing_balance
                    for record in other_payables_records
                    if record.closing_balance < 0
                )
            )
            if other_payables_records
            else None
        )
        deferred_tax_expense = _exact_sum(
            tuple(record.closing_balance for record in deferred_tax_records)
        )
        for field, amount in (
            ("other_payables_accrual", other_payables_accrual),
            ("sap_cumulative_deferred_tax_expense", deferred_tax_expense),
        ):
            if amount is not None and not fits_database_amount(amount):
                raise DgcSapAccountBalanceError(
                    "AGGREGATE_AMOUNT_OUT_OF_RANGE",
                    f"{field} must fit NUMERIC(38,12)",
                    field=field,
                )
        return DgcSapAccountBalanceResult(
            records=records,
            other_payables_records=other_payables_records,
            deferred_tax_records=deferred_tax_records,
            other_payables_accrual=other_payables_accrual,
            sap_cumulative_deferred_tax_expense=deferred_tax_expense,
            source_checksum=self._result.checksum,
        )

    def _parse_record(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> DgcSapAccountBalanceRecord:
        if any(not isinstance(key, str) for key in raw):
            raise DgcSapAccountBalanceError(
                "INVALID_RESPONSE_FIELD",
                "account-balance response field names must be strings",
                row_number=row_number,
            )
        expected = set(DGC_SAP_ACCOUNT_BALANCE_FIELDS)
        actual = set(raw)
        unexpected = tuple(sorted(actual - expected))
        if unexpected:
            raise DgcSapAccountBalanceError(
                "UNEXPECTED_RESPONSE_FIELD",
                "account-balance response row contains unexpected fields: "
                + ", ".join(unexpected),
                row_number=row_number,
                field=unexpected[0],
            )
        missing = tuple(sorted(expected - actual))
        if missing:
            raise DgcSapAccountBalanceError(
                "MISSING_RESPONSE_FIELD",
                "account-balance response row is missing required fields: "
                + ", ".join(missing),
                row_number=row_number,
                field=missing[0],
            )

        company_code = _text(raw["company_code"], "company_code", row_number)
        fiscal_year = _year(raw["fiscal_year"], row_number)
        fiscal_period = _period(raw["fiscal_period"], row_number)
        if company_code != self._expected_company_code:
            raise DgcSapAccountBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "account-balance response contained a company outside the requested scope",
                row_number=row_number,
                field="company_code",
            )
        if fiscal_year != self._expected_fiscal_year:
            raise DgcSapAccountBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "account-balance response contained a fiscal year outside the requested scope",
                row_number=row_number,
                field="fiscal_year",
            )
        if fiscal_period != self._expected_fiscal_period:
            raise DgcSapAccountBalanceError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "account-balance response contained a fiscal period outside the requested scope",
                row_number=row_number,
                field="fiscal_period",
            )

        return DgcSapAccountBalanceRecord(
            account_code=_text(raw["account_code"], "account_code", row_number),
            account_name=_text(raw["account_name"], "account_name", row_number),
            closing_balance=_amount(raw["closing_balance"], "closing_balance", row_number),
            company_code=company_code,
            company_name=_text(raw["company_name"], "company_name", row_number),
            credit_amount=_amount(raw["credit_amount"], "credit_amount", row_number),
            debit_amount=_amount(raw["debit_amount"], "debit_amount", row_number),
            fiscal_period=fiscal_period,
            fiscal_year=fiscal_year,
            input_tax_process_method=_optional_text(
                raw["input_tax_process_method"],
                "input_tax_process_method",
                row_number,
            ),
            net_amount=_amount(raw["net_amount"], "net_amount", row_number),
            opening_balance=_amount(raw["opening_balance"], "opening_balance", row_number),
            sfkf=_optional_text(raw["sfkf"], "sfkf", row_number),
        )


class DgcSapAccountBalanceMetricAdapter:
    """Materialize only metrics supported by evidence in the source response."""

    def __init__(
        self,
        result: DgcSapAccountBalanceResult,
        *,
        company_code: str,
        fiscal_year: int,
        fiscal_period: int,
        currency: str,
        amount_scale: int,
        extracted_at: datetime,
    ) -> None:
        if not isinstance(result, DgcSapAccountBalanceResult):
            raise TypeError("result must be a DgcSapAccountBalanceResult")
        company = _required_text(company_code, "company_code")
        year = _expected_year(fiscal_year)
        period_number = _expected_period(fiscal_period)
        period = date(year, period_number, monthrange(year, period_number)[1])
        metrics = (
            ("other_payables_accrual", result.other_payables_accrual),
            (
                "sap_cumulative_deferred_tax_expense",
                result.sap_cumulative_deferred_tax_expense,
            ),
        )
        rows: list[CanonicalFinancialRow] = []
        for metric_code, amount in metrics:
            if amount is None:
                continue
            identity = json.dumps(
                (company, str(year), f"{period_number:03d}", metric_code),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            rows.append(
                CanonicalFinancialRow(
                    source_record_key=(
                        f"dgc-sap-account-balance:{sha256(identity).hexdigest()}"
                    ),
                    company_code=company,
                    fiscal_year=year,
                    period=period,
                    currency=currency,
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


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _text(value: object, field: str, row_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be a non-empty string",
            row_number=row_number,
            field=field,
        )
    return value.strip()


def _optional_text(value: object, field: str, row_number: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be a string or null",
            row_number=row_number,
            field=field,
        )
    return value.strip()


def _expected_year(value: object) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise ValueError("fiscal_year must be an integer from 2000 to 9999") from error


def _year(value: object, row_number: int) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_year must be an integer from 2000 to 9999",
            row_number=row_number,
            field="fiscal_year",
        ) from error


def _integer_year(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            raise ValueError
        parsed = int(normalized)
    else:
        raise TypeError
    if not 2000 <= parsed <= 9999:
        raise ValueError
    return parsed


def _expected_period(value: object) -> int:
    try:
        return _integer_period(value)
    except (TypeError, ValueError) as error:
        raise ValueError("fiscal_period must be an integer from 1 to 12") from error


def _period(value: object, row_number: int) -> int:
    try:
        return _integer_period(value)
    except (TypeError, ValueError) as error:
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_period must be an integer from 1 to 12",
            row_number=row_number,
            field="fiscal_period",
        ) from error


def _integer_period(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if (
            not 1 <= len(normalized) <= 3
            or not normalized.isascii()
            or not normalized.isdecimal()
        ):
            raise ValueError
        parsed = int(normalized)
    else:
        raise TypeError
    if not 1 <= parsed <= 12:
        raise ValueError
    return parsed


def _amount(value: object, field: str, row_number: int) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise DgcSapAccountBalanceError(
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
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an exact decimal, integer, or decimal string",
            row_number=row_number,
            field=field,
        ) from error
    if not parsed.is_finite() or not fits_database_amount(parsed):
        raise DgcSapAccountBalanceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be finite and fit NUMERIC(38,12)",
            row_number=row_number,
            field=field,
        )
    return parsed


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
    "DEFERRED_TAX_ASSET_GL_ACCOUNT",
    "DGC_SAP_ACCOUNT_BALANCE_FIELDS",
    "DGC_SAP_ACCOUNT_BALANCE_TABLE_NAME",
    "OTHER_PAYABLES_ACCRUAL_ACCOUNTS",
    "DgcSapAccountBalanceAdapter",
    "DgcSapAccountBalanceError",
    "DgcSapAccountBalanceMetricAdapter",
    "DgcSapAccountBalanceRecord",
    "DgcSapAccountBalanceResult",
]

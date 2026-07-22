"""Strict response adapter for the DGC 汇算清缴相关科目明细 API."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from typing import Final, cast
from unicodedata import normalize

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    fits_database_amount,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


DIVIDEND_GL_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {
        "6111010000",
        "6111020000",
        "6111030000",
        "6111150000",
        "6111990000",
    }
)
DIVIDEND_KEYWORDS: Final[tuple[str, ...]] = ("分红", "股利", "利润分配")
OTHER_INCOME_GL_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {"6112010000", "6112020000", "6112040000"}
)
INCOME_TAX_EXPENSE_GL_ACCOUNTS: Final[frozenset[str]] = frozenset(
    {"6801010000", "6801020000", "6801030000"}
)
TAXES_PAYABLE_GL_ACCOUNTS: Final[frozenset[str]] = frozenset({"2221130000"})
DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME: Final[str] = "汇算清缴相关科目明细"
DGC_SAP_DIVIDEND_DETAIL_FIELDS: Final[tuple[str, ...]] = (
    "company",
    "companyname",
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
DGC_SAP_DIVIDEND_DETAIL_SCOPED_FIELDS: Final[tuple[str, ...]] = tuple(
    field for field in DGC_SAP_DIVIDEND_DETAIL_FIELDS if field not in {"company", "companyname"}
)
_OPTIONAL_TEXT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "header_text",
        "detail_text",
        "project_code",
        "project_name",
        "original_system_doc_no",
    }
)


class DgcSapDividendDetailError(ValueError):
    """A stable, row-aware failure raised for an unsafe dividend response."""

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
class DgcSapDividendDetailRecord:
    """One validated API row, preserving the published source field names."""

    company: str
    companyname: str | None
    fiscal_year: int
    fiscal_period: int
    voucher_no: str
    header_text: str
    detail_text: str
    amount_ksl: Decimal
    gl_account: str
    account_name: str
    project_code: str
    project_name: str
    debit_credit_flag: str
    group_currency: str
    original_system_doc_no: str


@dataclass(frozen=True, slots=True)
class DgcSapDividendDetailResult:
    """Filtered dividend evidence and its exact, unrounded KSL aggregation."""

    records: tuple[DgcSapDividendDetailRecord, ...]
    raw_ksl_total: Decimal
    cumulative_dividend_amount: Decimal
    currency: str | None
    match_count: int
    source_checksum: str


@dataclass(frozen=True, slots=True)
class DgcSettlementOtherIncomeResult:
    """Other-income rows and the exact workbook-defined negative-KSL amount."""

    records: tuple[DgcSapDividendDetailRecord, ...]
    raw_ksl_total: Decimal
    other_income_amount: Decimal
    currency: str | None
    match_count: int
    source_checksum: str


@dataclass(frozen=True, slots=True)
class DgcSettlementIncomeTaxExpenseLine:
    """One matched source row and its exact workbook-defined negative-KSL amount."""

    source_record: DgcSapDividendDetailRecord
    income_tax_expense_amount: Decimal


@dataclass(frozen=True, slots=True)
class DgcSettlementIncomeTaxExpenseResult:
    """Income-tax-expense details preserved as independent line amounts."""

    lines: tuple[DgcSettlementIncomeTaxExpenseLine, ...]
    match_count: int
    source_checksum: str


@dataclass(frozen=True, slots=True)
class DgcSettlementTaxesPayableLine:
    """One taxes-payable source row and its exact negative-KSL amount."""

    source_record: DgcSapDividendDetailRecord
    taxes_payable_amount: Decimal


@dataclass(frozen=True, slots=True)
class DgcSettlementTaxesPayableResult:
    """Taxes-payable details preserved as independent line amounts."""

    lines: tuple[DgcSettlementTaxesPayableLine, ...]
    match_count: int
    source_checksum: str


class DgcSapDividendDetailAdapter:
    """Validate, filter, and aggregate one company-year dividend response."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company: str,
        expected_fiscal_year: int | str,
        through_period: int = 12,
    ) -> None:
        if not isinstance(result, DgcFetchResult):
            raise TypeError("result must be a DgcFetchResult")
        self._result = result
        self._expected_company = _expected_company(expected_company)
        self._expected_fiscal_year = _expected_year(expected_fiscal_year)
        self._through_period = _expected_period(through_period)

    def adapt(self) -> DgcSapDividendDetailResult:
        """Return the qualifying detail and cumulative dividend amount."""

        validated = tuple(
            self._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        matched = tuple(
            record
            for record in validated
            if record.fiscal_period <= self._through_period and _is_dividend_record(record)
        )
        currencies = {record.group_currency for record in matched}
        if len(currencies) > 1:
            raise DgcSapDividendDetailError(
                "MIXED_MATCHED_CURRENCIES",
                "matched dividend rows must use one group currency",
                field="group_currency",
            )

        raw_total = _exact_sum(tuple(record.amount_ksl for record in matched))
        if not fits_database_amount(raw_total):
            raise DgcSapDividendDetailError(
                "AGGREGATE_AMOUNT_OUT_OF_RANGE",
                "cumulative dividend amount must fit NUMERIC(38,12)",
                field="amount_ksl",
            )
        return DgcSapDividendDetailResult(
            records=matched,
            raw_ksl_total=raw_total,
            cumulative_dividend_amount=(
                Decimal(0) if raw_total.is_zero() else raw_total.copy_negate()
            ),
            currency=next(iter(currencies), None),
            match_count=len(matched),
            source_checksum=self._result.checksum,
        )

    def _parse_record(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> DgcSapDividendDetailRecord:
        if any(not isinstance(key, str) for key in raw):
            raise DgcSapDividendDetailError(
                "INVALID_RESPONSE_FIELD",
                "dividend response field names must be strings",
                row_number=row_number,
            )
        actual_fields = set(raw)
        published_fields = set(DGC_SAP_DIVIDEND_DETAIL_FIELDS)
        scoped_fields = set(DGC_SAP_DIVIDEND_DETAIL_SCOPED_FIELDS)
        unexpected = tuple(sorted(actual_fields - published_fields))
        if unexpected:
            raise DgcSapDividendDetailError(
                "UNEXPECTED_RESPONSE_FIELD",
                f"dividend response row contains unexpected fields: {', '.join(unexpected)}",
                row_number=row_number,
                field=unexpected[0],
            )
        scoped_response = actual_fields == scoped_fields
        if actual_fields != published_fields and not scoped_response:
            required_fields = (
                published_fields if actual_fields & {"company", "companyname"} else scoped_fields
            )
            missing = tuple(sorted(required_fields - actual_fields))
            raise DgcSapDividendDetailError(
                "MISSING_RESPONSE_FIELD",
                f"dividend response row is missing required fields: {', '.join(missing)}",
                row_number=row_number,
                field=missing[0],
            )

        company = (
            self._expected_company
            if scoped_response
            else _text(raw["company"], "company", row_number=row_number)
        )
        companyname = (
            None
            if scoped_response
            else _text(raw["companyname"], "companyname", row_number=row_number)
        )
        fiscal_year = _year(raw["fiscal_year"], row_number=row_number)
        fiscal_period = _period(raw["fiscal_period"], row_number=row_number)
        if company != self._expected_company:
            raise DgcSapDividendDetailError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "dividend response contained a company outside the requested scope",
                row_number=row_number,
                field="company",
            )
        if fiscal_year != self._expected_fiscal_year:
            raise DgcSapDividendDetailError(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "dividend response contained a fiscal year outside the requested scope",
                row_number=row_number,
                field="fiscal_year",
            )

        text_values = {
            field: _text(
                raw[field],
                field,
                row_number=row_number,
                allow_blank=field in _OPTIONAL_TEXT_FIELDS,
            )
            for field in DGC_SAP_DIVIDEND_DETAIL_FIELDS
            if field
            not in {
                "company",
                "companyname",
                "fiscal_year",
                "fiscal_period",
                "amount_ksl",
            }
        }
        currency = text_values["group_currency"].upper()
        return DgcSapDividendDetailRecord(
            company=company,
            companyname=companyname,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            voucher_no=text_values["voucher_no"],
            header_text=text_values["header_text"],
            detail_text=text_values["detail_text"],
            amount_ksl=_amount(raw["amount_ksl"], row_number=row_number),
            gl_account=text_values["gl_account"],
            account_name=text_values["account_name"],
            project_code=text_values["project_code"],
            project_name=text_values["project_name"],
            debit_credit_flag=text_values["debit_credit_flag"],
            group_currency=currency,
            original_system_doc_no=text_values["original_system_doc_no"],
        )


class DgcSettlementOtherIncomeAdapter:
    """Extract the three configured other-income accounts from the same source."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company: str,
        expected_fiscal_year: int | str,
        through_period: int = 12,
    ) -> None:
        self._result = result
        self._through_period = _expected_period(through_period)
        self._row_adapter = DgcSapDividendDetailAdapter(
            result,
            expected_company=expected_company,
            expected_fiscal_year=expected_fiscal_year,
            through_period=through_period,
        )

    def adapt(self) -> DgcSettlementOtherIncomeResult:
        validated = tuple(
            self._row_adapter._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        matched = tuple(
            record
            for record in validated
            if record.fiscal_period <= self._through_period
            and record.gl_account in OTHER_INCOME_GL_ACCOUNTS
        )
        currencies = {record.group_currency for record in matched}
        if len(currencies) > 1:
            raise DgcSapDividendDetailError(
                "MIXED_MATCHED_CURRENCIES",
                "matched other-income rows must use one group currency",
                field="group_currency",
            )
        raw_total = _exact_sum(tuple(record.amount_ksl for record in matched))
        if not fits_database_amount(raw_total):
            raise DgcSapDividendDetailError(
                "AGGREGATE_AMOUNT_OUT_OF_RANGE",
                "other-income amount must fit NUMERIC(38,12)",
                field="amount_ksl",
            )
        return DgcSettlementOtherIncomeResult(
            records=matched,
            raw_ksl_total=raw_total,
            other_income_amount=(Decimal(0) if raw_total.is_zero() else raw_total.copy_negate()),
            currency=next(iter(currencies), None),
            match_count=len(matched),
            source_checksum=self._result.checksum,
        )


class DgcSettlementIncomeTaxExpenseAdapter:
    """Extract and sign-reverse each configured income-tax-expense detail."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company: str,
        expected_fiscal_year: int | str,
        through_period: int = 12,
    ) -> None:
        self._result = result
        self._through_period = _expected_period(through_period)
        self._row_adapter = DgcSapDividendDetailAdapter(
            result,
            expected_company=expected_company,
            expected_fiscal_year=expected_fiscal_year,
            through_period=through_period,
        )

    def adapt(self) -> DgcSettlementIncomeTaxExpenseResult:
        validated = tuple(
            self._row_adapter._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        matched = tuple(
            record
            for record in validated
            if record.fiscal_period <= self._through_period
            and record.gl_account in INCOME_TAX_EXPENSE_GL_ACCOUNTS
        )
        lines = tuple(
            DgcSettlementIncomeTaxExpenseLine(
                source_record=record,
                income_tax_expense_amount=(
                    Decimal(0) if record.amount_ksl.is_zero() else record.amount_ksl.copy_negate()
                ),
            )
            for record in matched
        )
        return DgcSettlementIncomeTaxExpenseResult(
            lines=lines,
            match_count=len(lines),
            source_checksum=self._result.checksum,
        )


class DgcSettlementTaxesPayableAdapter:
    """Extract and sign-reverse each corporate-income-tax payable detail."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        expected_company: str,
        expected_fiscal_year: int | str,
        through_period: int = 12,
    ) -> None:
        self._result = result
        self._through_period = _expected_period(through_period)
        self._row_adapter = DgcSapDividendDetailAdapter(
            result,
            expected_company=expected_company,
            expected_fiscal_year=expected_fiscal_year,
            through_period=through_period,
        )

    def adapt(self) -> DgcSettlementTaxesPayableResult:
        validated = tuple(
            self._row_adapter._parse_record(raw, row_number)
            for row_number, raw in enumerate(self._result.records, start=1)
        )
        matched = tuple(
            record
            for record in validated
            if record.fiscal_period <= self._through_period
            and record.gl_account in TAXES_PAYABLE_GL_ACCOUNTS
        )
        lines = tuple(
            DgcSettlementTaxesPayableLine(
                source_record=record,
                taxes_payable_amount=(
                    Decimal(0) if record.amount_ksl.is_zero() else record.amount_ksl.copy_negate()
                ),
            )
            for record in matched
        )
        return DgcSettlementTaxesPayableResult(
            lines=lines,
            match_count=len(lines),
            source_checksum=self._result.checksum,
        )


class DgcSapDividendMetricAdapter:
    """Materialize one validated cumulative dividend as a quarterly metric row."""

    METRIC_CODE: Final[str] = "received_dividends"

    def __init__(
        self,
        result: DgcSapDividendDetailResult,
        *,
        company_code: str,
        fiscal_year: int,
        through_period: int,
        currency: str,
        amount_scale: int,
        extracted_at: datetime,
    ) -> None:
        if not isinstance(result, DgcSapDividendDetailResult):
            raise TypeError("result must be a DgcSapDividendDetailResult")
        normalized_company = _expected_company(company_code)
        normalized_year = _expected_year(fiscal_year)
        normalized_period = _expected_period(through_period)
        normalized_currency = _metric_currency(currency)
        if result.currency is not None and result.currency != normalized_currency:
            raise DgcSapDividendDetailError(
                "DGC_RESPONSE_CURRENCY_MISMATCH",
                "matched dividend currency does not match the requested batch currency",
                field="group_currency",
            )
        period = date(
            normalized_year,
            normalized_period,
            monthrange(normalized_year, normalized_period)[1],
        )
        identity = json.dumps(
            (
                normalized_company,
                str(normalized_year),
                f"{normalized_period:02d}",
                self.METRIC_CODE,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._checksum = result.source_checksum
        self._row = CanonicalFinancialRow(
            source_record_key=(f"dgc-sap-dividend-detail:{sha256(identity).hexdigest()}"),
            company_code=normalized_company,
            fiscal_year=normalized_year,
            period=period,
            currency=normalized_currency,
            amount_scale=amount_scale,
            metric_code=self.METRIC_CODE,
            amount=result.cumulative_dividend_amount,
            extracted_at=extracted_at,
        )

    @property
    def checksum(self) -> str:
        return self._checksum

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        yield AdapterRow(row_number=1, value=self._row, error=None)


def _expected_company(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected_company must be a non-empty string")
    return value.strip()


def _expected_year(value: object) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise ValueError("expected_fiscal_year must be an integer from 2000 to 9999") from error


def _expected_period(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 12:
        raise ValueError("through_period must be an integer from 1 to 12")
    return value


def _year(value: object, *, row_number: int) -> int:
    try:
        return _integer_year(value)
    except (TypeError, ValueError) as error:
        raise DgcSapDividendDetailError(
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


def _period(value: object, *, row_number: int) -> int:
    if not isinstance(value, str):
        raise DgcSapDividendDetailError(
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
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            "fiscal_period must be a string from 1/01/001 through 12/012",
            row_number=row_number,
            field="fiscal_period",
        )
    return int(normalized)


def _metric_currency(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("currency must be a string")
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be three ASCII letters")
    return normalized


def _text(
    value: object,
    field: str,
    *,
    row_number: int,
    allow_blank: bool = False,
) -> str:
    if value is None and allow_blank:
        return ""
    if not isinstance(value, str):
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be a string",
            row_number=row_number,
            field=field,
        )
    parsed = value.strip()
    if not parsed and not allow_blank:
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must not be blank",
            row_number=row_number,
            field=field,
        )
    return parsed


def _amount(value: object, *, row_number: int) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            "amount_ksl must be an exact decimal, integer, or decimal string",
            row_number=row_number,
            field="amount_ksl",
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
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            "amount_ksl must be an exact decimal, integer, or decimal string",
            row_number=row_number,
            field="amount_ksl",
        ) from error
    if not parsed.is_finite():
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            "amount_ksl must be finite",
            row_number=row_number,
            field="amount_ksl",
        )
    if not fits_database_amount(parsed):
        raise DgcSapDividendDetailError(
            "INVALID_RESPONSE_VALUE",
            "amount_ksl must fit NUMERIC(38,12)",
            row_number=row_number,
            field="amount_ksl",
        )
    return parsed


def _is_dividend_record(record: DgcSapDividendDetailRecord) -> bool:
    if record.gl_account not in DIVIDEND_GL_ACCOUNTS:
        return False
    normalized_header = normalize("NFKC", record.header_text)
    normalized_detail = normalize("NFKC", record.detail_text)
    return any(
        keyword in normalized_header or keyword in normalized_detail
        for keyword in DIVIDEND_KEYWORDS
    )


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
    "DGC_SAP_DIVIDEND_DETAIL_FIELDS",
    "DGC_SAP_DIVIDEND_DETAIL_SCOPED_FIELDS",
    "DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME",
    "DIVIDEND_GL_ACCOUNTS",
    "DIVIDEND_KEYWORDS",
    "INCOME_TAX_EXPENSE_GL_ACCOUNTS",
    "OTHER_INCOME_GL_ACCOUNTS",
    "TAXES_PAYABLE_GL_ACCOUNTS",
    "DgcSapDividendDetailAdapter",
    "DgcSapDividendDetailError",
    "DgcSapDividendDetailRecord",
    "DgcSapDividendDetailResult",
    "DgcSapDividendMetricAdapter",
    "DgcSettlementIncomeTaxExpenseAdapter",
    "DgcSettlementIncomeTaxExpenseLine",
    "DgcSettlementIncomeTaxExpenseResult",
    "DgcSettlementOtherIncomeAdapter",
    "DgcSettlementOtherIncomeResult",
    "DgcSettlementTaxesPayableAdapter",
    "DgcSettlementTaxesPayableLine",
    "DgcSettlementTaxesPayableResult",
]

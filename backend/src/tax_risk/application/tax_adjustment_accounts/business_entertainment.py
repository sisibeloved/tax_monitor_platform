from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.application.tax_adjustment_accounts.contracts import (
    CheckStatus,
    CurrencyCheckSummary,
    MonthlyCheckSummary,
    SettlementAdjustmentRow,
)


BUSINESS_ENTERTAINMENT_ACCOUNT = "6600400000"
EMPLOYEE_WELFARE_KEYWORDS = (
    "内部会议餐",
    "培训餐",
    "员工聚餐",
    "团建",
    "年会",
    "加班餐",
    "食堂",
    "员工福利",
)
MEETING_OR_EDUCATION_KEYWORDS = (
    "会议通知",
    "签到",
    "议程",
    "培训班",
)


class BusinessEntertainmentLabel(StrEnum):
    REASONABLE = "BUSINESS_ENTERTAINMENT_REASONABLE"
    EMPLOYEE_WELFARE = "BUSINESS_ENTERTAINMENT_EMPLOYEE_WELFARE"
    MEETING_OR_EDUCATION = "BUSINESS_ENTERTAINMENT_MEETING_OR_EDUCATION"


class BusinessEntertainmentEvidenceSource(StrEnum):
    SETTLEMENT_DETAIL_TEXT = "SETTLEMENT_DETAIL_TEXT"
    HESI_DETAIL_DESCRIPTION = "HESI_DETAIL_DESCRIPTION"
    HESI_INVOICE_LINK = "HESI_INVOICE_LINK"
    HESI_APPLICATION_DESCRIPTION = "HESI_APPLICATION_DESCRIPTION"
    RULE_CHAIN_COMPLETE = "RULE_CHAIN_COMPLETE"


class BusinessEntertainmentCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company: str = Field(min_length=1, max_length=64)
    fiscal_year: str = Field(pattern=r"^\d{4}$")
    through_month: int = Field(ge=1, le=12)


class HesiDetailRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    document_code: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=8000)

    @field_validator("description", mode="before")
    @classmethod
    def blank_description_is_empty(cls, value: object) -> object:
        return "" if value is None else value


class HesiInvoiceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    invoice_id: str = Field(default="", max_length=512)
    reception_apply_code: str = Field(default="", max_length=128)

    @field_validator("invoice_id", "reception_apply_code", mode="before")
    @classmethod
    def blank_link_field_is_empty(cls, value: object) -> object:
        return "" if value is None else value


class HesiApplicationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=8000)

    @field_validator("description", mode="before")
    @classmethod
    def blank_description_is_empty(cls, value: object) -> object:
        return "" if value is None else value


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentRuleDecision:
    status: CheckStatus
    labels: tuple[BusinessEntertainmentLabel, ...]
    matched_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentCheckedDetail:
    row: SettlementAdjustmentRow
    status: CheckStatus
    labels: tuple[BusinessEntertainmentLabel, ...]
    matched_keywords: tuple[str, ...]
    decision_source: BusinessEntertainmentEvidenceSource
    evaluated_sources: tuple[BusinessEntertainmentEvidenceSource, ...]
    evidence_texts: tuple[str, ...]
    hesi_document_code: str | None
    hesi_detail_match_count: int
    hesi_invoice_match_count: int
    reception_apply_codes: tuple[str, ...]
    hesi_application_match_count: int
    hesi_detail_descriptions: tuple[str, ...] = ()
    hesi_application_descriptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentCheckResult:
    request: BusinessEntertainmentCheckRequest
    source_row_count: int
    in_scope_source_row_count: int
    eligible_detail_count: int
    details: tuple[BusinessEntertainmentCheckedDetail, ...]
    currency_summaries: tuple[CurrencyCheckSummary, ...]
    monthly_summaries: tuple[MonthlyCheckSummary, ...]
    hesi_detail_source_row_count: int
    hesi_invoice_source_row_count: int
    hesi_application_source_row_count: int


class SettlementAdjustmentSource(Protocol):
    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> Sequence[SettlementAdjustmentRow]: ...


class HesiDetailSource(Protocol):
    def fetch_rows(self, *, company_code: str) -> Sequence[HesiDetailRow]: ...


class HesiInvoiceSource(Protocol):
    def fetch_rows(self, *, company_code: str) -> Sequence[HesiInvoiceRow]: ...


class HesiApplicationSource(Protocol):
    def fetch_rows(self, *, company_code: str) -> Sequence[HesiApplicationRow]: ...


def business_entertainment_account_is_in_scope(gl_account: str) -> bool:
    return gl_account.strip() == BUSINESS_ENTERTAINMENT_ACCOUNT


def classify_business_entertainment_text(text: str) -> BusinessEntertainmentRuleDecision:
    labels: list[BusinessEntertainmentLabel] = []
    matched_keywords: list[str] = []

    welfare_hits = [keyword for keyword in EMPLOYEE_WELFARE_KEYWORDS if keyword in text]
    if welfare_hits:
        labels.append(BusinessEntertainmentLabel.EMPLOYEE_WELFARE)
        matched_keywords.extend(welfare_hits)

    meeting_hits = [keyword for keyword in MEETING_OR_EDUCATION_KEYWORDS if keyword in text]
    if meeting_hits:
        labels.append(BusinessEntertainmentLabel.MEETING_OR_EDUCATION)
        matched_keywords.extend(meeting_hits)

    if not labels:
        return BusinessEntertainmentRuleDecision(
            status=CheckStatus.NORMAL,
            labels=(BusinessEntertainmentLabel.REASONABLE,),
            matched_keywords=(),
        )
    return BusinessEntertainmentRuleDecision(
        status=CheckStatus.ABNORMAL,
        labels=tuple(labels),
        matched_keywords=tuple(dict.fromkeys(matched_keywords)),
    )


def extract_hesi_document_code(original_system_doc_no: str) -> str | None:
    normalized = original_system_doc_no.strip()
    if len(normalized) <= 2 or normalized[:2].upper() != "HS":
        return None
    code = normalized[2:].strip()
    return code or None


class BusinessEntertainmentAccountCheckService:
    def __init__(
        self,
        *,
        settlement_source: SettlementAdjustmentSource,
        hesi_detail_source: HesiDetailSource,
        hesi_invoice_source: HesiInvoiceSource,
        hesi_application_source: HesiApplicationSource,
    ) -> None:
        self._settlement_source = settlement_source
        self._hesi_detail_source = hesi_detail_source
        self._hesi_invoice_source = hesi_invoice_source
        self._hesi_application_source = hesi_application_source

    def run(
        self,
        request: BusinessEntertainmentCheckRequest,
    ) -> BusinessEntertainmentCheckResult:
        source_rows = tuple(
            self._settlement_source.fetch_rows(
                company=request.company,
                fiscal_year=request.fiscal_year,
            )
        )
        self._validate_settlement_scope(source_rows, request)
        in_scope_rows = tuple(
            row for row in source_rows if 1 <= int(row.fiscal_period) <= request.through_month
        )
        eligible_rows = tuple(
            sorted(
                (
                    row
                    for row in in_scope_rows
                    if business_entertainment_account_is_in_scope(row.gl_account)
                ),
                key=lambda row: (
                    row.fiscal_period,
                    row.voucher_no,
                    row.original_system_doc_no,
                ),
            )
        )

        detail_rows: tuple[HesiDetailRow, ...] | None = None
        invoice_rows: tuple[HesiInvoiceRow, ...] | None = None
        application_rows: tuple[HesiApplicationRow, ...] | None = None
        detail_index: dict[str, tuple[HesiDetailRow, ...]] = {}
        invoice_index: dict[str, tuple[HesiInvoiceRow, ...]] = {}
        application_index: dict[str, tuple[HesiApplicationRow, ...]] = {}

        def load_details() -> dict[str, tuple[HesiDetailRow, ...]]:
            nonlocal detail_rows, detail_index
            if detail_rows is None:
                detail_rows = tuple(
                    self._hesi_detail_source.fetch_rows(company_code=request.company)
                )
                self._validate_company_scope(detail_rows, request.company, "hesimingxi")
                detail_index = _index_rows(detail_rows, lambda row: row.document_code)
            return detail_index

        def load_invoices() -> dict[str, tuple[HesiInvoiceRow, ...]]:
            nonlocal invoice_rows, invoice_index
            if invoice_rows is None:
                invoice_rows = tuple(
                    self._hesi_invoice_source.fetch_rows(company_code=request.company)
                )
                self._validate_company_scope(invoice_rows, request.company, "hesiinvoice")
                invoice_index = _index_rows(invoice_rows, lambda row: row.code)
            return invoice_index

        def load_applications() -> dict[str, tuple[HesiApplicationRow, ...]]:
            nonlocal application_rows, application_index
            if application_rows is None:
                application_rows = tuple(
                    self._hesi_application_source.fetch_rows(company_code=request.company)
                )
                self._validate_company_scope(application_rows, request.company, "apply")
                application_index = _index_rows(application_rows, lambda row: row.code)
            return application_index

        checked_details: list[BusinessEntertainmentCheckedDetail] = []
        for row in eligible_rows:
            settlement_decision = classify_business_entertainment_text(row.detail_text)
            document_code = extract_hesi_document_code(row.original_system_doc_no)
            if document_code is None:
                checked_details.append(
                    _checked_detail(
                        row=row,
                        decision=settlement_decision,
                        decision_source=(
                            BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT
                        ),
                        evaluated_sources=(
                            BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT,
                        ),
                        evidence_texts=(row.detail_text,) if row.detail_text else (),
                    )
                )
                continue

            evaluated_sources = [
                BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT,
                BusinessEntertainmentEvidenceSource.HESI_DETAIL_DESCRIPTION,
            ]
            matched_detail_rows = load_details().get(document_code, ())
            detail_descriptions = _unique_nonblank(item.description for item in matched_detail_rows)
            detail_decision = _classify_texts(detail_descriptions)
            evaluated_sources.append(BusinessEntertainmentEvidenceSource.HESI_INVOICE_LINK)
            matched_invoice_rows = load_invoices().get(document_code, ())
            reception_apply_codes = _unique_nonblank(
                item.reception_apply_code for item in matched_invoice_rows
            )
            matched_application_rows: tuple[HesiApplicationRow, ...] = ()
            application_descriptions: tuple[str, ...] = ()
            application_decision = _reasonable_decision()
            if reception_apply_codes:
                evaluated_sources.append(
                    BusinessEntertainmentEvidenceSource.HESI_APPLICATION_DESCRIPTION
                )
                applications = load_applications()
                matched_application_rows = tuple(
                    application
                    for code in reception_apply_codes
                    for application in applications.get(code, ())
                )
                application_descriptions = _unique_nonblank(
                    item.description for item in matched_application_rows
                )
                application_decision = _classify_texts(application_descriptions)

            evidence_texts: tuple[str, ...]
            if settlement_decision.status is CheckStatus.ABNORMAL:
                decision_source = BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT
                final_decision = settlement_decision
                evidence_texts = (row.detail_text,) if row.detail_text else ()
            elif detail_decision.status is CheckStatus.ABNORMAL:
                decision_source = BusinessEntertainmentEvidenceSource.HESI_DETAIL_DESCRIPTION
                final_decision = detail_decision
                evidence_texts = detail_descriptions
            elif application_decision.status is CheckStatus.ABNORMAL:
                decision_source = BusinessEntertainmentEvidenceSource.HESI_APPLICATION_DESCRIPTION
                final_decision = application_decision
                evidence_texts = application_descriptions
            else:
                decision_source = BusinessEntertainmentEvidenceSource.RULE_CHAIN_COMPLETE
                final_decision = _reasonable_decision()
                evidence_texts = _unique_nonblank((*detail_descriptions, *application_descriptions))

            checked_details.append(
                _checked_detail(
                    row=row,
                    decision=final_decision,
                    decision_source=decision_source,
                    evaluated_sources=tuple(evaluated_sources),
                    evidence_texts=evidence_texts,
                    hesi_document_code=document_code,
                    hesi_detail_match_count=len(matched_detail_rows),
                    hesi_invoice_match_count=len(matched_invoice_rows),
                    reception_apply_codes=reception_apply_codes,
                    hesi_application_match_count=len(matched_application_rows),
                    hesi_detail_descriptions=detail_descriptions,
                    hesi_application_descriptions=application_descriptions,
                )
            )

        details = tuple(checked_details)
        return BusinessEntertainmentCheckResult(
            request=request,
            source_row_count=len(source_rows),
            in_scope_source_row_count=len(in_scope_rows),
            eligible_detail_count=len(eligible_rows),
            details=details,
            currency_summaries=_currency_summaries(details),
            monthly_summaries=_monthly_summaries(details, request.through_month),
            hesi_detail_source_row_count=len(detail_rows or ()),
            hesi_invoice_source_row_count=len(invoice_rows or ()),
            hesi_application_source_row_count=len(application_rows or ()),
        )

    @staticmethod
    def _validate_settlement_scope(
        rows: tuple[SettlementAdjustmentRow, ...],
        request: BusinessEntertainmentCheckRequest,
    ) -> None:
        if any(row.company != request.company for row in rows):
            raise ValueError("settlement_adjustment returned a row outside the company scope")
        if any(row.fiscal_year != request.fiscal_year for row in rows):
            raise ValueError("settlement_adjustment returned a row outside the fiscal year")

    @staticmethod
    def _validate_company_scope(
        rows: Sequence[HesiDetailRow | HesiInvoiceRow | HesiApplicationRow],
        company: str,
        source_name: str,
    ) -> None:
        if any(row.company_code != company for row in rows):
            raise ValueError(f"{source_name} returned a row outside the company scope")


def _reasonable_decision() -> BusinessEntertainmentRuleDecision:
    return BusinessEntertainmentRuleDecision(
        status=CheckStatus.NORMAL,
        labels=(BusinessEntertainmentLabel.REASONABLE,),
        matched_keywords=(),
    )


def _classify_texts(texts: Sequence[str]) -> BusinessEntertainmentRuleDecision:
    labels: list[BusinessEntertainmentLabel] = []
    keywords: list[str] = []
    for text in texts:
        decision = classify_business_entertainment_text(text)
        if decision.status is CheckStatus.ABNORMAL:
            labels.extend(decision.labels)
            keywords.extend(decision.matched_keywords)
    if not labels:
        return _reasonable_decision()
    return BusinessEntertainmentRuleDecision(
        status=CheckStatus.ABNORMAL,
        labels=tuple(dict.fromkeys(labels)),
        matched_keywords=tuple(dict.fromkeys(keywords)),
    )


def _checked_detail(
    *,
    row: SettlementAdjustmentRow,
    decision: BusinessEntertainmentRuleDecision,
    decision_source: BusinessEntertainmentEvidenceSource,
    evaluated_sources: tuple[BusinessEntertainmentEvidenceSource, ...],
    evidence_texts: tuple[str, ...],
    hesi_document_code: str | None = None,
    hesi_detail_match_count: int = 0,
    hesi_invoice_match_count: int = 0,
    reception_apply_codes: tuple[str, ...] = (),
    hesi_application_match_count: int = 0,
    hesi_detail_descriptions: tuple[str, ...] = (),
    hesi_application_descriptions: tuple[str, ...] = (),
) -> BusinessEntertainmentCheckedDetail:
    return BusinessEntertainmentCheckedDetail(
        row=row,
        status=decision.status,
        labels=decision.labels,
        matched_keywords=decision.matched_keywords,
        decision_source=decision_source,
        evaluated_sources=evaluated_sources,
        evidence_texts=evidence_texts,
        hesi_document_code=hesi_document_code,
        hesi_detail_match_count=hesi_detail_match_count,
        hesi_invoice_match_count=hesi_invoice_match_count,
        reception_apply_codes=reception_apply_codes,
        hesi_application_match_count=hesi_application_match_count,
        hesi_detail_descriptions=hesi_detail_descriptions,
        hesi_application_descriptions=hesi_application_descriptions,
    )


def _index_rows[RowT](
    rows: Sequence[RowT],
    key: Callable[[RowT], str],
) -> dict[str, tuple[RowT, ...]]:
    grouped: dict[str, list[RowT]] = defaultdict(list)
    for row in rows:
        grouped[key(row).strip()].append(row)
    return {code: tuple(items) for code, items in grouped.items()}


def _unique_nonblank(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _currency_summaries(
    details: tuple[BusinessEntertainmentCheckedDetail, ...],
) -> tuple[CurrencyCheckSummary, ...]:
    grouped: dict[str, list[BusinessEntertainmentCheckedDetail]] = defaultdict(list)
    for detail in details:
        grouped[detail.row.group_currency].append(detail)
    return tuple(
        CurrencyCheckSummary(
            currency=currency,
            detail_count=len(rows),
            amount=_sum_amount(rows),
            normal_count=sum(row.status is CheckStatus.NORMAL for row in rows),
            normal_amount=_sum_amount(row for row in rows if row.status is CheckStatus.NORMAL),
            abnormal_count=sum(row.status is CheckStatus.ABNORMAL for row in rows),
            abnormal_amount=_sum_amount(row for row in rows if row.status is CheckStatus.ABNORMAL),
        )
        for currency, rows in sorted(grouped.items())
    )


def _monthly_summaries(
    details: tuple[BusinessEntertainmentCheckedDetail, ...],
    through_month: int,
) -> tuple[MonthlyCheckSummary, ...]:
    currencies = sorted({detail.row.group_currency for detail in details})
    grouped: dict[tuple[int, str], list[BusinessEntertainmentCheckedDetail]] = defaultdict(list)
    for detail in details:
        grouped[(int(detail.row.fiscal_period), detail.row.group_currency)].append(detail)
    summaries: list[MonthlyCheckSummary] = []
    for month in range(1, through_month + 1):
        for currency in currencies:
            rows = grouped[(month, currency)]
            summaries.append(
                MonthlyCheckSummary(
                    month=month,
                    currency=currency,
                    detail_count=len(rows),
                    amount=_sum_amount(rows),
                    normal_count=sum(row.status is CheckStatus.NORMAL for row in rows),
                    normal_amount=_sum_amount(
                        row for row in rows if row.status is CheckStatus.NORMAL
                    ),
                    abnormal_count=sum(row.status is CheckStatus.ABNORMAL for row in rows),
                    abnormal_amount=_sum_amount(
                        row for row in rows if row.status is CheckStatus.ABNORMAL
                    ),
                )
            )
    return tuple(summaries)


def _sum_amount(rows: Iterable[BusinessEntertainmentCheckedDetail]) -> Decimal:
    return sum((row.row.amount_ksl for row in rows), Decimal("0"))


__all__ = [
    "BUSINESS_ENTERTAINMENT_ACCOUNT",
    "BusinessEntertainmentAccountCheckService",
    "BusinessEntertainmentCheckRequest",
    "BusinessEntertainmentCheckResult",
    "BusinessEntertainmentCheckedDetail",
    "BusinessEntertainmentEvidenceSource",
    "BusinessEntertainmentLabel",
    "BusinessEntertainmentRuleDecision",
    "EMPLOYEE_WELFARE_KEYWORDS",
    "HesiApplicationRow",
    "HesiApplicationSource",
    "HesiDetailRow",
    "HesiDetailSource",
    "HesiInvoiceRow",
    "HesiInvoiceSource",
    "MEETING_OR_EDUCATION_KEYWORDS",
    "SettlementAdjustmentSource",
    "business_entertainment_account_is_in_scope",
    "classify_business_entertainment_text",
    "extract_hesi_document_code",
]

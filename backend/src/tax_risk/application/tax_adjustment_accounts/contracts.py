from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdjustmentSubject(StrEnum):
    WELFARE = "WELFARE"
    DONATION = "DONATION"


class CheckStatus(StrEnum):
    NORMAL = "NORMAL"
    ABNORMAL = "ABNORMAL"


class AdjustmentLabel(StrEnum):
    WELFARE_REASONABLE = "WELFARE_REASONABLE"
    WELFARE_BUSINESS_ENTERTAINMENT = "WELFARE_BUSINESS_ENTERTAINMENT"
    WELFARE_EMPLOYEE_EDUCATION = "WELFARE_EMPLOYEE_EDUCATION"
    WELFARE_ADVERTISING_PROMOTION = "WELFARE_ADVERTISING_PROMOTION"
    WELFARE_CUSTOMER_GIFT_REVIEW = "WELFARE_CUSTOMER_GIFT_REVIEW"
    DONATION_REASONABLE = "DONATION_REASONABLE"
    DONATION_SPONSORSHIP = "DONATION_SPONSORSHIP"
    DONATION_ADVERTISING_PROMOTION = "DONATION_ADVERTISING_PROMOTION"


class AccountCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    subject: AdjustmentSubject
    company: str = Field(min_length=1, max_length=64)
    fiscal_year: str = Field(pattern=r"^\d{4}$")
    through_month: int = Field(ge=1, le=12)


class SettlementAdjustmentRow(BaseModel):
    """One company-scoped row returned by settlement_adjustment."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company: str = Field(min_length=1, max_length=64)
    companyname: str | None = Field(default=None, max_length=256)
    fiscal_year: str = Field(pattern=r"^\d{4}$")
    fiscal_period: str = Field(pattern=r"^\d{3}$")
    voucher_no: str = Field(min_length=1, max_length=64)
    header_text: str = Field(default="", max_length=2000)
    detail_text: str = Field(default="", max_length=4000)
    amount_ksl: Decimal
    gl_account: str = Field(pattern=r"^\d+$", max_length=32)
    account_name: str = Field(default="", max_length=512)
    project_code: str = Field(default="", max_length=128)
    project_name: str = Field(default="", max_length=512)
    debit_credit_flag: str = Field(default="", max_length=32)
    group_currency: str = Field(min_length=1, max_length=16)
    original_system_doc_no: str = Field(default="", max_length=128)

    @field_validator("amount_ksl")
    @classmethod
    def amount_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount_ksl must be finite")
        return value


class TrialBalanceRow(BaseModel):
    """One company-period row returned by trial_balance."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    company_name: str | None = Field(default=None, max_length=256)
    fiscal_year: str = Field(pattern=r"^\d{4}$")
    fiscal_period: str = Field(pattern=r"^\d{3}$")
    gl_account_code: str = Field(pattern=r"^\d+$", max_length=32)
    gl_account_name: str | None = Field(default=None, max_length=512)
    bank_center_code: str | None = Field(default=None, max_length=128)
    bank_account_number: str | None = Field(default=None, max_length=128)
    cost_center_code: str | None = Field(default=None, max_length=128)
    cost_center_name: str | None = Field(default=None, max_length=512)
    profit_center_code: str | None = Field(default=None, max_length=128)
    profit_center_name: str | None = Field(default=None, max_length=512)
    internal_order_code: str | None = Field(default=None, max_length=128)
    internal_order_name: str | None = Field(default=None, max_length=512)
    business_area_code: str | None = Field(default=None, max_length=128)
    business_area_name: str | None = Field(default=None, max_length=512)
    customer_code: str | None = Field(default=None, max_length=128)
    customer_name: str | None = Field(default=None, max_length=512)
    vendor_code: str | None = Field(default=None, max_length=128)
    vendor_name: str | None = Field(default=None, max_length=512)
    asset_code: str | None = Field(default=None, max_length=128)
    asset_name: str | None = Field(default=None, max_length=512)
    rstgr: str | None = Field(default=None, max_length=128)
    rstgr_name: str | None = Field(default=None, max_length=512)
    input_tax_process_method: str | None = Field(default=None, max_length=128)
    sfkf: str | None = Field(default=None, max_length=128)
    total_debit_amount: Decimal
    total_credit_amount: Decimal

    @field_validator("total_debit_amount", "total_credit_amount", mode="before")
    @classmethod
    def blank_amount_is_zero(cls, value: object) -> object:
        return Decimal("0") if value is None or value == "" else value

    @field_validator("total_debit_amount", "total_credit_amount")
    @classmethod
    def trial_balance_amount_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("trial balance amount must be finite")
        return value


class SapIncomeRow(BaseModel):
    """One company-period row returned by sapincome."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    mandt: str = Field(min_length=1, max_length=16)
    bukrs: str = Field(min_length=1, max_length=64)
    companyname: str | None = Field(default=None, max_length=256)
    gjahr: str = Field(pattern=r"^\d{4}$")
    monat: str = Field(pattern=r"^(0[1-9]|1[0-2])$")
    rldnr: str = Field(min_length=1, max_length=32)
    hs: str = Field(min_length=1, max_length=32)
    ztext: str = Field(min_length=1, max_length=512)
    nmhsl: Decimal
    nyhsl: Decimal

    @field_validator("nmhsl", "nyhsl", mode="before")
    @classmethod
    def blank_income_amount_is_zero(cls, value: object) -> object:
        return Decimal("0") if value is None or value == "" else value

    @field_validator("nmhsl", "nyhsl")
    @classmethod
    def income_amount_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("sapincome amount must be finite")
        return value


@dataclass(frozen=True, slots=True)
class CheckedAdjustmentDetail:
    row: SettlementAdjustmentRow
    status: CheckStatus
    labels: tuple[AdjustmentLabel, ...]
    matched_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CurrencyCheckSummary:
    currency: str
    detail_count: int
    amount: Decimal
    normal_count: int
    normal_amount: Decimal
    abnormal_count: int
    abnormal_amount: Decimal


@dataclass(frozen=True, slots=True)
class MonthlyCheckSummary:
    month: int
    currency: str
    detail_count: int
    amount: Decimal
    normal_count: int
    normal_amount: Decimal
    abnormal_count: int
    abnormal_amount: Decimal


@dataclass(frozen=True, slots=True)
class TaxAdjustmentDecision:
    cumulative_expense: Decimal
    cumulative_base: Decimal
    limit_rate: Decimal
    raw_adjustment_amount: Decimal
    adjustment_amount: Decimal
    detail_check_selected: bool


@dataclass(frozen=True, slots=True)
class WelfareAdjustmentMonthlySummary:
    month: int
    welfare_amount: Decimal
    cumulative_welfare_amount: Decimal
    salary_amount: Decimal
    cumulative_salary_amount: Decimal
    deduction_limit: Decimal
    adjustment_amount: Decimal


@dataclass(frozen=True, slots=True)
class WelfareAdjustmentResult:
    request: AccountCheckRequest
    monthly_summaries: tuple[WelfareAdjustmentMonthlySummary, ...]
    adjustment_amount: Decimal
    detail_check_selected: bool


@dataclass(frozen=True, slots=True)
class DonationAdjustmentResult:
    request: AccountCheckRequest
    cumulative_donation_amount: Decimal
    cumulative_profit_amount: Decimal
    deduction_limit: Decimal
    adjustment_amount: Decimal
    detail_check_selected: bool
    matched_profit_row_count: int


@dataclass(frozen=True, slots=True)
class AccountCheckResult:
    request: AccountCheckRequest
    source_row_count: int
    in_scope_source_row_count: int
    details: tuple[CheckedAdjustmentDetail, ...]
    currency_summaries: tuple[CurrencyCheckSummary, ...]
    monthly_summaries: tuple[MonthlyCheckSummary, ...]
    adjustment_amount: Decimal
    detail_check_selected: bool
    eligible_detail_count: int


__all__ = [
    "AccountCheckRequest",
    "AccountCheckResult",
    "AdjustmentLabel",
    "AdjustmentSubject",
    "CheckStatus",
    "CheckedAdjustmentDetail",
    "CurrencyCheckSummary",
    "DonationAdjustmentResult",
    "MonthlyCheckSummary",
    "SapIncomeRow",
    "SettlementAdjustmentRow",
    "TaxAdjustmentDecision",
    "TrialBalanceRow",
    "WelfareAdjustmentMonthlySummary",
    "WelfareAdjustmentResult",
]

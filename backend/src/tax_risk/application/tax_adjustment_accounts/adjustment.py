from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentSubject,
    DonationAdjustmentResult,
    SapIncomeRow,
    SettlementAdjustmentRow,
    TaxAdjustmentDecision,
    TrialBalanceRow,
    WelfareAdjustmentMonthlySummary,
    WelfareAdjustmentResult,
)
from tax_risk.application.tax_adjustment_accounts.rules import account_is_in_scope


WELFARE_LIMIT_RATE = Decimal("0.14")
DONATION_LIMIT_RATE = Decimal("0.12")
SALARY_ACCOUNT_MIN = 6_600_010_000
SALARY_ACCOUNT_MAX = 6_600_019_900
SALARY_EXCLUDED_ACCOUNTS = frozenset({6_600_010_700, 6_600_010_701})
PROFIT_TOTAL_LABEL = '四、利润总额(损失以"-"号填列)'
_PROFIT_LABEL_TRANSLATION = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "“": '"',
        "”": '"',
        "－": "-",
        "—": "-",
        "–": "-",
    }
)


def calculate_limited_tax_adjustment(
    *,
    cumulative_expense: Decimal,
    cumulative_base: Decimal,
    limit_rate: Decimal,
) -> TaxAdjustmentDecision:
    _require_finite(cumulative_expense, "cumulative_expense")
    _require_finite(cumulative_base, "cumulative_base")
    _require_finite(limit_rate, "limit_rate")
    if limit_rate < Decimal("0"):
        raise ValueError("limit_rate cannot be negative")

    raw_adjustment = cumulative_expense - cumulative_base * limit_rate
    adjustment = max(raw_adjustment, Decimal("0"))
    return TaxAdjustmentDecision(
        cumulative_expense=cumulative_expense,
        cumulative_base=cumulative_base,
        limit_rate=limit_rate,
        raw_adjustment_amount=raw_adjustment,
        adjustment_amount=adjustment,
        detail_check_selected=adjustment > Decimal("0"),
    )


def salary_account_is_in_scope(gl_account_code: str) -> bool:
    normalized = gl_account_code.strip()
    if not normalized.isdigit():
        return False
    account = int(normalized)
    return (
        SALARY_ACCOUNT_MIN <= account <= SALARY_ACCOUNT_MAX
        and account not in SALARY_EXCLUDED_ACCOUNTS
    )


def profit_total_label_matches(value: str) -> bool:
    normalized = "".join(value.translate(_PROFIT_LABEL_TRANSLATION).split())
    return normalized == PROFIT_TOTAL_LABEL


def calculate_welfare_adjustment(
    request: AccountCheckRequest,
    *,
    settlement_rows: Sequence[SettlementAdjustmentRow],
    trial_balance_rows_by_month: Mapping[int, Sequence[TrialBalanceRow]],
) -> WelfareAdjustmentResult:
    if request.subject is not AdjustmentSubject.WELFARE:
        raise ValueError("welfare adjustment requires a WELFARE request")

    expected_months = set(range(1, request.through_month + 1))
    if set(trial_balance_rows_by_month) != expected_months:
        raise ValueError("trial balance rows must cover every requested month exactly once")

    welfare_by_month = {month: Decimal("0") for month in expected_months}
    for settlement_row in settlement_rows:
        _validate_settlement_scope(settlement_row, request)
        month = int(settlement_row.fiscal_period)
        if month in expected_months and account_is_in_scope(
            AdjustmentSubject.WELFARE,
            settlement_row.gl_account,
        ):
            welfare_by_month[month] += settlement_row.amount_ksl

    salary_by_month = {month: Decimal("0") for month in expected_months}
    for month, rows in trial_balance_rows_by_month.items():
        expected_period = f"{month:03d}"
        for trial_row in rows:
            if trial_row.company_code != request.company:
                raise ValueError("trial_balance returned a row outside the company scope")
            if trial_row.fiscal_year != request.fiscal_year:
                raise ValueError("trial_balance returned a row outside the fiscal year")
            if trial_row.fiscal_period != expected_period:
                raise ValueError("trial_balance returned a row outside the fiscal period")
            if salary_account_is_in_scope(trial_row.gl_account_code):
                salary_by_month[month] += (
                    trial_row.total_debit_amount + trial_row.total_credit_amount
                )

    cumulative_welfare = Decimal("0")
    cumulative_salary = Decimal("0")
    monthly_summaries: list[WelfareAdjustmentMonthlySummary] = []
    for month in range(1, request.through_month + 1):
        cumulative_welfare += welfare_by_month[month]
        cumulative_salary += salary_by_month[month]
        decision = calculate_limited_tax_adjustment(
            cumulative_expense=cumulative_welfare,
            cumulative_base=cumulative_salary,
            limit_rate=WELFARE_LIMIT_RATE,
        )
        monthly_summaries.append(
            WelfareAdjustmentMonthlySummary(
                month=month,
                welfare_amount=welfare_by_month[month],
                cumulative_welfare_amount=cumulative_welfare,
                salary_amount=salary_by_month[month],
                cumulative_salary_amount=cumulative_salary,
                deduction_limit=cumulative_salary * WELFARE_LIMIT_RATE,
                adjustment_amount=decision.adjustment_amount,
            )
        )

    final_summary = monthly_summaries[-1]
    return WelfareAdjustmentResult(
        request=request,
        monthly_summaries=tuple(monthly_summaries),
        adjustment_amount=final_summary.adjustment_amount,
        detail_check_selected=final_summary.adjustment_amount > Decimal("0"),
    )


def calculate_donation_adjustment(
    request: AccountCheckRequest,
    *,
    settlement_rows: Sequence[SettlementAdjustmentRow],
    sap_income_rows: Sequence[SapIncomeRow],
) -> DonationAdjustmentResult:
    if request.subject is not AdjustmentSubject.DONATION:
        raise ValueError("donation adjustment requires a DONATION request")

    cumulative_donation = Decimal("0")
    for settlement_row in settlement_rows:
        _validate_settlement_scope(settlement_row, request)
        if 1 <= int(settlement_row.fiscal_period) <= request.through_month and account_is_in_scope(
            AdjustmentSubject.DONATION,
            settlement_row.gl_account,
        ):
            cumulative_donation += settlement_row.amount_ksl

    expected_period = f"{request.through_month:02d}"
    cumulative_profit = Decimal("0")
    matched_profit_row_count = 0
    for income_row in sap_income_rows:
        if income_row.bukrs != request.company:
            raise ValueError("sapincome returned a row outside the company scope")
        if income_row.gjahr != request.fiscal_year:
            raise ValueError("sapincome returned a row outside the fiscal year")
        if income_row.monat != expected_period:
            raise ValueError("sapincome returned a row outside the fiscal period")
        if profit_total_label_matches(income_row.ztext):
            cumulative_profit += income_row.nyhsl
            matched_profit_row_count += 1

    if matched_profit_row_count == 0:
        raise ValueError("sapincome cumulative profit row is missing")

    decision = calculate_limited_tax_adjustment(
        cumulative_expense=cumulative_donation,
        cumulative_base=cumulative_profit,
        limit_rate=DONATION_LIMIT_RATE,
    )
    return DonationAdjustmentResult(
        request=request,
        cumulative_donation_amount=cumulative_donation,
        cumulative_profit_amount=cumulative_profit,
        deduction_limit=cumulative_profit * DONATION_LIMIT_RATE,
        adjustment_amount=decision.adjustment_amount,
        detail_check_selected=decision.detail_check_selected,
        matched_profit_row_count=matched_profit_row_count,
    )


def _validate_settlement_scope(
    row: SettlementAdjustmentRow,
    request: AccountCheckRequest,
) -> None:
    if row.company != request.company:
        raise ValueError("settlement_adjustment returned a row outside the company scope")
    if row.fiscal_year != request.fiscal_year:
        raise ValueError("settlement_adjustment returned a row outside the fiscal year")


def _require_finite(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")


__all__ = [
    "DONATION_LIMIT_RATE",
    "PROFIT_TOTAL_LABEL",
    "SALARY_ACCOUNT_MAX",
    "SALARY_ACCOUNT_MIN",
    "SALARY_EXCLUDED_ACCOUNTS",
    "WELFARE_LIMIT_RATE",
    "calculate_donation_adjustment",
    "calculate_limited_tax_adjustment",
    "calculate_welfare_adjustment",
    "profit_total_label_matches",
    "salary_account_is_in_scope",
]

from decimal import Decimal

import pytest

from tax_risk.application.tax_adjustment_accounts.adjustment import (
    calculate_limited_tax_adjustment,
    calculate_welfare_adjustment,
    salary_account_is_in_scope,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentSubject,
    SettlementAdjustmentRow,
    TrialBalanceRow,
)


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ("6600010000", True),
        ("6600019900", True),
        ("6600010700", False),
        ("6600010701", False),
        ("6600009999", False),
        ("6600019901", False),
        ("invalid", False),
    ],
)
def test_salary_account_scope(account: str, expected: bool) -> None:
    assert salary_account_is_in_scope(account) is expected


def test_limited_adjustment_is_clamped_at_zero() -> None:
    result = calculate_limited_tax_adjustment(
        cumulative_expense=Decimal("100"),
        cumulative_base=Decimal("1000"),
        limit_rate=Decimal("0.14"),
    )

    assert result.raw_adjustment_amount == Decimal("-40.00")
    assert result.adjustment_amount == Decimal("0")
    assert result.detail_check_selected is False


def test_welfare_adjustment_is_cumulative_and_excludes_salary_accounts() -> None:
    request = AccountCheckRequest(
        subject=AdjustmentSubject.WELFARE,
        company="3HD0",
        fiscal_year="2025",
        through_month=2,
    )
    settlement_rows = (
        _welfare_row("001", "100", "1"),
        _welfare_row("002", "150", "2"),
        _welfare_row("003", "999", "3"),
    )
    trial_rows = {
        1: (
            _salary_row("001", "6600010000", "1000", "-100"),
            _salary_row("001", "6600010700", "9999", "0"),
        ),
        2: (
            _salary_row("002", "6600019900", "500", "0"),
            _salary_row("002", "6600010701", "9999", "0"),
        ),
    }

    result = calculate_welfare_adjustment(
        request,
        settlement_rows=settlement_rows,
        trial_balance_rows_by_month=trial_rows,
    )

    assert result.monthly_summaries[0].cumulative_salary_amount == Decimal("900")
    assert result.monthly_summaries[0].adjustment_amount == Decimal("0")
    assert result.monthly_summaries[1].cumulative_welfare_amount == Decimal("250")
    assert result.monthly_summaries[1].cumulative_salary_amount == Decimal("1400")
    assert result.monthly_summaries[1].deduction_limit == Decimal("196.00")
    assert result.adjustment_amount == Decimal("54.00")
    assert result.detail_check_selected is True


def test_welfare_adjustment_requires_every_month() -> None:
    request = AccountCheckRequest(
        subject=AdjustmentSubject.WELFARE,
        company="3HD0",
        fiscal_year="2025",
        through_month=2,
    )

    with pytest.raises(ValueError, match="every requested month"):
        calculate_welfare_adjustment(
            request,
            settlement_rows=(),
            trial_balance_rows_by_month={1: ()},
        )


def _welfare_row(period: str, amount: str, voucher: str) -> SettlementAdjustmentRow:
    return SettlementAdjustmentRow(
        company="3HD0",
        fiscal_year="2025",
        fiscal_period=period,
        voucher_no=voucher,
        detail_text="员工福利",
        amount_ksl=Decimal(amount),
        gl_account="6600080000",
        account_name="福利费",
        group_currency="CNY",
    )


def _salary_row(
    period: str,
    account: str,
    debit: str,
    credit: str,
) -> TrialBalanceRow:
    return TrialBalanceRow(
        company_code="3HD0",
        fiscal_year="2025",
        fiscal_period=period,
        gl_account_code=account,
        total_debit_amount=Decimal(debit),
        total_credit_amount=Decimal(credit),
    )

from decimal import Decimal

import pytest

from tax_risk.application.tax_adjustment_accounts.adjustment import (
    calculate_donation_adjustment,
    profit_total_label_matches,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentSubject,
    SapIncomeRow,
    SettlementAdjustmentRow,
)


@pytest.mark.parametrize(
    "label",
    [
        '四、利润总额(损失以"-"号填列)',
        "四、利润总额（损失以“－”号填列）",
        " 四、利润总额（损失以“－”号填列） ",
    ],
)
def test_profit_total_label_accepts_valid_punctuation_variants(label: str) -> None:
    assert profit_total_label_matches(label) is True


def test_profit_total_label_rejects_broad_or_different_items() -> None:
    assert profit_total_label_matches("利润总额") is False
    assert profit_total_label_matches("三、营业利润") is False


def test_donation_adjustment_uses_cumulative_expense_and_profit_sum() -> None:
    result = calculate_donation_adjustment(
        _request(),
        settlement_rows=(
            _donation_row("001", "1000", "1"),
            _donation_row("003", "500", "2"),
            _donation_row("004", "999", "3"),
            _donation_row("003", "200", "4", account="6600080000"),
        ),
        sap_income_rows=(
            _income_row("10000", "四、利润总额（损失以“－”号填列）"),
            _income_row("1000", '四、利润总额(损失以"-"号填列)', hs="29"),
            _income_row("999999", "三、营业利润", hs="27"),
        ),
    )

    assert result.cumulative_donation_amount == Decimal("1500")
    assert result.cumulative_profit_amount == Decimal("11000")
    assert result.deduction_limit == Decimal("1320.00")
    assert result.adjustment_amount == Decimal("180.00")
    assert result.detail_check_selected is True
    assert result.matched_profit_row_count == 2


def test_donation_adjustment_is_clamped_at_zero() -> None:
    result = calculate_donation_adjustment(
        _request(),
        settlement_rows=(_donation_row("001", "100", "1"),),
        sap_income_rows=(_income_row("1000", "四、利润总额（损失以“－”号填列）"),),
    )

    assert result.adjustment_amount == Decimal("0")
    assert result.detail_check_selected is False


def test_missing_profit_total_is_not_treated_as_zero() -> None:
    with pytest.raises(ValueError, match="profit row is missing"):
        calculate_donation_adjustment(
            _request(),
            settlement_rows=(_donation_row("001", "100", "1"),),
            sap_income_rows=(_income_row("1000", "三、营业利润"),),
        )


def _request() -> AccountCheckRequest:
    return AccountCheckRequest(
        subject=AdjustmentSubject.DONATION,
        company="3HD0",
        fiscal_year="2025",
        through_month=3,
    )


def _donation_row(
    period: str,
    amount: str,
    voucher: str,
    *,
    account: str = "6711060000",
) -> SettlementAdjustmentRow:
    return SettlementAdjustmentRow(
        company="3HD0",
        fiscal_year="2025",
        fiscal_period=period,
        voucher_no=voucher,
        detail_text="公益性捐赠",
        amount_ksl=Decimal(amount),
        gl_account=account,
        account_name="公益性捐赠",
        group_currency="CNY",
    )


def _income_row(amount: str, label: str, *, hs: str = "28") -> SapIncomeRow:
    return SapIncomeRow(
        mandt="800",
        bukrs="3HD0",
        companyname="测试公司",
        gjahr="2025",
        monat="03",
        rldnr="0L",
        hs=hs,
        ztext=label,
        nmhsl=Decimal("0"),
        nyhsl=Decimal(amount),
    )

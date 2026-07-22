from decimal import Decimal

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentLabel,
    AdjustmentSubject,
    SettlementAdjustmentRow,
    TrialBalanceRow,
)
from tax_risk.application.tax_adjustment_accounts.welfare import (
    WelfareAdjustmentAccountCheckService,
)


class SettlementSource:
    def __init__(self, amount: str) -> None:
        self.amount = amount
        self.calls = 0

    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> tuple[SettlementAdjustmentRow, ...]:
        self.calls += 1
        return (
            SettlementAdjustmentRow(
                company=company,
                fiscal_year=fiscal_year,
                fiscal_period="001",
                voucher_no="1",
                detail_text="客户商务宴请",
                amount_ksl=Decimal(self.amount),
                gl_account="6600080000",
                account_name="福利费",
                group_currency="CNY",
            ),
        )


class TrialSource:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_rows(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
    ) -> tuple[TrialBalanceRow, ...]:
        self.calls.append(fiscal_period)
        return (
            TrialBalanceRow(
                company_code=company_code,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                gl_account_code="6600010000",
                total_debit_amount=Decimal("1000"),
                total_credit_amount=Decimal("0"),
            ),
        )


def _request() -> AccountCheckRequest:
    return AccountCheckRequest(
        subject=AdjustmentSubject.WELFARE,
        company="3HD0",
        fiscal_year="2025",
        through_month=1,
    )


def test_zero_welfare_adjustment_skips_detail_labels() -> None:
    settlement = SettlementSource("100")
    trial = TrialSource()

    result = WelfareAdjustmentAccountCheckService(
        settlement_source=settlement,
        trial_balance_source=trial,
    ).run(_request())

    assert settlement.calls == 1
    assert trial.calls == ["001"]
    assert result.adjustment.adjustment_amount == Decimal("0")
    assert result.account_check.detail_check_selected is False
    assert result.account_check.eligible_detail_count == 1
    assert result.account_check.details == ()


def test_positive_welfare_adjustment_checks_detail_labels() -> None:
    result = WelfareAdjustmentAccountCheckService(
        settlement_source=SettlementSource("200"),
        trial_balance_source=TrialSource(),
    ).run(_request())

    assert result.adjustment.adjustment_amount == Decimal("60.00")
    assert result.account_check.detail_check_selected is True
    assert result.account_check.details[0].labels == (
        AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT,
    )

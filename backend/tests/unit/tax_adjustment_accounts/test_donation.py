from decimal import Decimal

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentLabel,
    AdjustmentSubject,
    SapIncomeRow,
    SettlementAdjustmentRow,
)
from tax_risk.application.tax_adjustment_accounts.donation import (
    DonationAdjustmentAccountCheckService,
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
                fiscal_period="003",
                voucher_no="1",
                detail_text="公益项目赞助",
                amount_ksl=Decimal(self.amount),
                gl_account="6711060000",
                account_name="公益性捐赠",
                group_currency="CNY",
            ),
        )


class IncomeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def fetch_rows(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
    ) -> tuple[SapIncomeRow, ...]:
        self.calls.append((company_code, fiscal_year, fiscal_period))
        return (
            SapIncomeRow(
                mandt="800",
                bukrs=company_code,
                companyname="测试公司",
                gjahr=fiscal_year,
                monat=fiscal_period,
                rldnr="0L",
                hs="28",
                ztext="四、利润总额（损失以“－”号填列）",
                nmhsl=Decimal("0"),
                nyhsl=Decimal("1000"),
            ),
        )


def _request() -> AccountCheckRequest:
    return AccountCheckRequest(
        subject=AdjustmentSubject.DONATION,
        company="3HD0",
        fiscal_year="2025",
        through_month=3,
    )


def test_zero_donation_adjustment_skips_detail_labels() -> None:
    settlement = SettlementSource("100")
    income = IncomeSource()

    result = DonationAdjustmentAccountCheckService(
        settlement_source=settlement,
        sap_income_source=income,
    ).run(_request())

    assert settlement.calls == 1
    assert income.calls == [("3HD0", "2025", "03")]
    assert result.adjustment.adjustment_amount == Decimal("0")
    assert result.account_check.detail_check_selected is False
    assert result.account_check.eligible_detail_count == 1
    assert result.account_check.details == ()


def test_positive_donation_adjustment_checks_detail_labels() -> None:
    result = DonationAdjustmentAccountCheckService(
        settlement_source=SettlementSource("200"),
        sap_income_source=IncomeSource(),
    ).run(_request())

    assert result.adjustment.adjustment_amount == Decimal("80.00")
    assert result.account_check.detail_check_selected is True
    assert result.account_check.details[0].labels == (AdjustmentLabel.DONATION_SPONSORSHIP,)

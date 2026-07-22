from decimal import Decimal

import pytest

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AdjustmentLabel,
    AdjustmentSubject,
    CheckStatus,
    SettlementAdjustmentRow,
)
from tax_risk.application.tax_adjustment_accounts.service import (
    TaxAdjustmentAccountCheckService,
)


class FakeSource:
    def __init__(self, rows: tuple[SettlementAdjustmentRow, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> tuple[SettlementAdjustmentRow, ...]:
        self.calls.append((company, fiscal_year))
        return self.rows


def _row(
    *,
    period: str,
    account: str,
    amount: str,
    detail: str,
    voucher: str,
    company: str = "3320",
    year: str = "2025",
    currency: str = "CNY",
) -> SettlementAdjustmentRow:
    return SettlementAdjustmentRow(
        company=company,
        fiscal_year=year,
        fiscal_period=period,
        voucher_no=voucher,
        header_text="",
        detail_text=detail,
        amount_ksl=Decimal(amount),
        gl_account=account,
        account_name="测试科目",
        project_code="",
        project_name="",
        debit_credit_flag="S",
        group_currency=currency,
        original_system_doc_no=f"source-{voucher}",
    )


def _request(subject: AdjustmentSubject, through_month: int = 3) -> AccountCheckRequest:
    return AccountCheckRequest(
        subject=subject,
        company="3320",
        fiscal_year="2025",
        through_month=through_month,
    )


def test_nonpositive_adjustment_skips_source_and_detail_check() -> None:
    source = FakeSource(
        (
            _row(
                period="001",
                account="6711060000",
                amount="100",
                detail="项目赞助",
                voucher="1",
            ),
        )
    )
    service = TaxAdjustmentAccountCheckService(source)

    result = service.check(
        _request(AdjustmentSubject.DONATION),
        adjustment_amount=Decimal("0"),
    )

    assert source.calls == []
    assert result.detail_check_selected is False
    assert result.adjustment_amount == Decimal("0")
    assert result.details == ()


def test_negative_adjustment_is_normalized_to_zero() -> None:
    result = TaxAdjustmentAccountCheckService(FakeSource(())).check(
        _request(AdjustmentSubject.WELFARE),
        adjustment_amount=Decimal("-0.01"),
    )

    assert result.adjustment_amount == Decimal("0")
    assert result.detail_check_selected is False


def test_preloaded_zero_adjustment_counts_eligible_rows_without_classifying() -> None:
    rows = (
        _row(
            period="001",
            account="6600080000",
            amount="100",
            detail="客户商务宴请",
            voucher="1",
        ),
    )

    result = TaxAdjustmentAccountCheckService(FakeSource(())).check_rows(
        _request(AdjustmentSubject.WELFARE),
        source_rows=rows,
        adjustment_amount=Decimal("0"),
    )

    assert result.eligible_detail_count == 1
    assert result.details == ()
    assert result.currency_summaries == ()


def test_donation_check_keeps_normal_and_abnormal_details_when_selected() -> None:
    source = FakeSource(
        (
            _row(
                period="001", account="6711060000", amount="100.10", detail="公益捐赠", voucher="1"
            ),
            _row(
                period="002", account="6711060000", amount="20.20", detail="项目赞助", voucher="2"
            ),
            _row(period="004", account="6711060000", amount="999", detail="后续月份", voucher="3"),
            _row(period="002", account="6600080000", amount="50", detail="非捐赠科目", voucher="4"),
        )
    )
    service = TaxAdjustmentAccountCheckService(source)

    result = service.check(
        _request(AdjustmentSubject.DONATION),
        adjustment_amount=Decimal("0.01"),
    )

    assert source.calls == [("3320", "2025")]
    assert result.detail_check_selected is True
    assert result.source_row_count == 4
    assert result.in_scope_source_row_count == 3
    assert result.eligible_detail_count == 2
    assert len(result.details) == 2
    assert result.details[0].status is CheckStatus.NORMAL
    assert result.details[1].labels == (AdjustmentLabel.DONATION_SPONSORSHIP,)
    summary = result.currency_summaries[0]
    assert summary.amount == Decimal("120.30")
    assert summary.normal_amount == Decimal("100.10")
    assert summary.abnormal_amount == Decimal("20.20")
    assert len(result.monthly_summaries) == 3
    assert result.monthly_summaries[2].amount == Decimal("0")


def test_welfare_check_uses_signed_amount_ksl_and_account_boundaries() -> None:
    source = FakeSource(
        (
            _row(
                period="001", account="6600080000", amount="10.01", detail="年度体检", voucher="1"
            ),
            _row(
                period="001", account="6600089900", amount="-2.00", detail="员工培训费", voucher="2"
            ),
            _row(period="001", account="6600089901", amount="500", detail="客户宴请", voucher="3"),
        )
    )

    result = TaxAdjustmentAccountCheckService(source).check(
        _request(AdjustmentSubject.WELFARE, through_month=1),
        adjustment_amount=Decimal("1"),
    )

    assert len(result.details) == 2
    assert result.currency_summaries[0].amount == Decimal("8.01")
    assert result.currency_summaries[0].abnormal_amount == Decimal("-2.00")


def test_source_rows_cannot_escape_requested_scope() -> None:
    source = FakeSource(
        (
            _row(
                period="001",
                account="6711060000",
                amount="1",
                detail="捐赠",
                voucher="1",
                company="OTHER",
            ),
        )
    )

    with pytest.raises(ValueError, match="company scope"):
        TaxAdjustmentAccountCheckService(source).check(
            _request(AdjustmentSubject.DONATION),
            adjustment_amount=Decimal("1"),
        )

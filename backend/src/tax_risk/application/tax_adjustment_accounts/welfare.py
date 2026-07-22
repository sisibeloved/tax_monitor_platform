from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tax_risk.application.tax_adjustment_accounts.adjustment import (
    calculate_welfare_adjustment,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AccountCheckResult,
    AdjustmentSubject,
    TrialBalanceRow,
    WelfareAdjustmentResult,
)
from tax_risk.application.tax_adjustment_accounts.service import (
    SettlementAdjustmentSource,
    TaxAdjustmentAccountCheckService,
)


class TrialBalanceSource(Protocol):
    def fetch_rows(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
    ) -> Sequence[TrialBalanceRow]: ...


@dataclass(frozen=True, slots=True)
class WelfareAdjustmentCheckResult:
    adjustment: WelfareAdjustmentResult
    account_check: AccountCheckResult


class WelfareAdjustmentAccountCheckService:
    def __init__(
        self,
        *,
        settlement_source: SettlementAdjustmentSource,
        trial_balance_source: TrialBalanceSource,
    ) -> None:
        self._settlement_source = settlement_source
        self._trial_balance_source = trial_balance_source
        self._account_checker = TaxAdjustmentAccountCheckService(settlement_source)

    def run(self, request: AccountCheckRequest) -> WelfareAdjustmentCheckResult:
        if request.subject is not AdjustmentSubject.WELFARE:
            raise ValueError("welfare workflow requires a WELFARE request")

        settlement_rows = tuple(
            self._settlement_source.fetch_rows(
                company=request.company,
                fiscal_year=request.fiscal_year,
            )
        )
        trial_balance_rows_by_month = {
            month: tuple(
                self._trial_balance_source.fetch_rows(
                    company_code=request.company,
                    fiscal_year=request.fiscal_year,
                    fiscal_period=f"{month:03d}",
                )
            )
            for month in range(1, request.through_month + 1)
        }
        adjustment = calculate_welfare_adjustment(
            request,
            settlement_rows=settlement_rows,
            trial_balance_rows_by_month=trial_balance_rows_by_month,
        )
        account_check = self._account_checker.check_rows(
            request,
            source_rows=settlement_rows,
            adjustment_amount=adjustment.adjustment_amount,
        )
        return WelfareAdjustmentCheckResult(
            adjustment=adjustment,
            account_check=account_check,
        )


__all__ = [
    "TrialBalanceSource",
    "WelfareAdjustmentAccountCheckService",
    "WelfareAdjustmentCheckResult",
]

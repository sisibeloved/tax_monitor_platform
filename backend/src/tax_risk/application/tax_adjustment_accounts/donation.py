from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tax_risk.application.tax_adjustment_accounts.adjustment import (
    calculate_donation_adjustment,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AccountCheckResult,
    AdjustmentSubject,
    DonationAdjustmentResult,
    SapIncomeRow,
)
from tax_risk.application.tax_adjustment_accounts.service import (
    SettlementAdjustmentSource,
    TaxAdjustmentAccountCheckService,
)


class SapIncomeSource(Protocol):
    def fetch_rows(
        self,
        *,
        company_code: str,
        fiscal_year: str,
        fiscal_period: str,
    ) -> Sequence[SapIncomeRow]: ...


@dataclass(frozen=True, slots=True)
class DonationAdjustmentCheckResult:
    adjustment: DonationAdjustmentResult
    account_check: AccountCheckResult


class DonationAdjustmentAccountCheckService:
    def __init__(
        self,
        *,
        settlement_source: SettlementAdjustmentSource,
        sap_income_source: SapIncomeSource,
    ) -> None:
        self._settlement_source = settlement_source
        self._sap_income_source = sap_income_source
        self._account_checker = TaxAdjustmentAccountCheckService(settlement_source)

    def run(self, request: AccountCheckRequest) -> DonationAdjustmentCheckResult:
        if request.subject is not AdjustmentSubject.DONATION:
            raise ValueError("donation workflow requires a DONATION request")

        settlement_rows = tuple(
            self._settlement_source.fetch_rows(
                company=request.company,
                fiscal_year=request.fiscal_year,
            )
        )
        sap_income_rows = tuple(
            self._sap_income_source.fetch_rows(
                company_code=request.company,
                fiscal_year=request.fiscal_year,
                fiscal_period=f"{request.through_month:02d}",
            )
        )
        adjustment = calculate_donation_adjustment(
            request,
            settlement_rows=settlement_rows,
            sap_income_rows=sap_income_rows,
        )
        account_check = self._account_checker.check_rows(
            request,
            source_rows=settlement_rows,
            adjustment_amount=adjustment.adjustment_amount,
        )
        return DonationAdjustmentCheckResult(
            adjustment=adjustment,
            account_check=account_check,
        )


__all__ = [
    "DonationAdjustmentAccountCheckService",
    "DonationAdjustmentCheckResult",
    "SapIncomeSource",
]

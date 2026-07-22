from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Protocol

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AccountCheckRequest,
    AccountCheckResult,
    CheckStatus,
    CheckedAdjustmentDetail,
    CurrencyCheckSummary,
    MonthlyCheckSummary,
    SettlementAdjustmentRow,
)
from tax_risk.application.tax_adjustment_accounts.rules import (
    account_is_in_scope,
    classify_detail,
)


class SettlementAdjustmentSource(Protocol):
    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> Sequence[SettlementAdjustmentRow]: ...


class TaxAdjustmentAccountCheckService:
    def __init__(self, source: SettlementAdjustmentSource) -> None:
        self._source = source

    def check(
        self,
        request: AccountCheckRequest,
        *,
        adjustment_amount: Decimal,
    ) -> AccountCheckResult:
        normalized_adjustment = _normalize_adjustment_amount(adjustment_amount)
        if normalized_adjustment == Decimal("0"):
            return _empty_result(request, normalized_adjustment)

        source_rows = tuple(
            self._source.fetch_rows(
                company=request.company,
                fiscal_year=request.fiscal_year,
            )
        )
        return self.check_rows(
            request,
            source_rows=source_rows,
            adjustment_amount=normalized_adjustment,
        )

    def check_rows(
        self,
        request: AccountCheckRequest,
        *,
        source_rows: Sequence[SettlementAdjustmentRow],
        adjustment_amount: Decimal,
    ) -> AccountCheckResult:
        normalized_adjustment = _normalize_adjustment_amount(adjustment_amount)
        rows = tuple(source_rows)
        self._validate_scope(rows, request)
        in_scope_rows = tuple(
            row for row in rows if 1 <= int(row.fiscal_period) <= request.through_month
        )
        eligible_rows = tuple(
            row for row in in_scope_rows if account_is_in_scope(request.subject, row.gl_account)
        )
        if normalized_adjustment == Decimal("0"):
            return AccountCheckResult(
                request=request,
                source_row_count=len(rows),
                in_scope_source_row_count=len(in_scope_rows),
                details=(),
                currency_summaries=(),
                monthly_summaries=(),
                adjustment_amount=normalized_adjustment,
                detail_check_selected=False,
                eligible_detail_count=len(eligible_rows),
            )

        details = tuple(
            sorted(
                (self._classify(row, request) for row in eligible_rows),
                key=lambda detail: (
                    detail.row.fiscal_period,
                    detail.row.voucher_no,
                    detail.row.original_system_doc_no,
                ),
            )
        )
        return AccountCheckResult(
            request=request,
            source_row_count=len(rows),
            in_scope_source_row_count=len(in_scope_rows),
            details=details,
            currency_summaries=_currency_summaries(details),
            monthly_summaries=_monthly_summaries(details, request.through_month),
            adjustment_amount=normalized_adjustment,
            detail_check_selected=True,
            eligible_detail_count=len(eligible_rows),
        )

    @staticmethod
    def _validate_scope(
        rows: tuple[SettlementAdjustmentRow, ...],
        request: AccountCheckRequest,
    ) -> None:
        if any(row.company != request.company for row in rows):
            raise ValueError("settlement_adjustment returned a row outside the company scope")
        if any(row.fiscal_year != request.fiscal_year for row in rows):
            raise ValueError("settlement_adjustment returned a row outside the fiscal year")

    @staticmethod
    def _classify(
        row: SettlementAdjustmentRow,
        request: AccountCheckRequest,
    ) -> CheckedAdjustmentDetail:
        decision = classify_detail(request.subject, row.detail_text)
        return CheckedAdjustmentDetail(
            row=row,
            status=decision.status,
            labels=decision.labels,
            matched_keywords=decision.matched_keywords,
        )


def _empty_result(
    request: AccountCheckRequest,
    adjustment_amount: Decimal,
) -> AccountCheckResult:
    return AccountCheckResult(
        request=request,
        source_row_count=0,
        in_scope_source_row_count=0,
        details=(),
        currency_summaries=(),
        monthly_summaries=(),
        adjustment_amount=adjustment_amount,
        detail_check_selected=False,
        eligible_detail_count=0,
    )


def _normalize_adjustment_amount(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("adjustment_amount must be a finite Decimal")
    return max(value, Decimal("0"))


def _currency_summaries(
    details: tuple[CheckedAdjustmentDetail, ...],
) -> tuple[CurrencyCheckSummary, ...]:
    grouped: dict[str, list[CheckedAdjustmentDetail]] = defaultdict(list)
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
    details: tuple[CheckedAdjustmentDetail, ...],
    through_month: int,
) -> tuple[MonthlyCheckSummary, ...]:
    currencies = sorted({detail.row.group_currency for detail in details})
    grouped: dict[tuple[int, str], list[CheckedAdjustmentDetail]] = defaultdict(list)
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


def _sum_amount(rows: Iterable[CheckedAdjustmentDetail]) -> Decimal:
    return sum((row.row.amount_ksl for row in rows), Decimal("0"))


__all__ = ["SettlementAdjustmentSource", "TaxAdjustmentAccountCheckService"]

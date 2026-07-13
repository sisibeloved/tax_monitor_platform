from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class MissingScopeInput(ValueError):
    """Raised when an approved scope formula input is absent."""


class DuplicateScopeMetric(ValueError):
    """Raised when a frozen snapshot contains more than one scope metric."""


@dataclass(frozen=True, slots=True)
class ScopeInput:
    company_code: str
    period: str
    cumulative_expense: Decimal | None
    cumulative_base: Decimal | None
    limit_rate: Decimal


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    company_code: str
    period: str
    adjustment: Decimal
    selected: bool


def evaluate_scope(value: ScopeInput) -> ScopeDecision:
    if value.cumulative_expense is None:
        raise MissingScopeInput("cumulative_expense is required")
    if value.cumulative_base is None:
        raise MissingScopeInput("cumulative_base is required")
    adjustment = value.cumulative_expense - value.cumulative_base * value.limit_rate
    return ScopeDecision(
        company_code=value.company_code,
        period=value.period,
        adjustment=adjustment,
        selected=adjustment > Decimal("0"),
    )


__all__ = [
    "DuplicateScopeMetric",
    "MissingScopeInput",
    "ScopeDecision",
    "ScopeInput",
    "evaluate_scope",
]

"""Pure, deterministic quarterly corporate-income-tax calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, ROUND_HALF_UP
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Mapping, TypeAlias, cast

from tax_risk.domain.money import Money, Rate


class CalculationStatus(StrEnum):
    """Independent outcome status for each quarterly monitor."""

    CALCULATED = "CALCULATED"
    NOT_CALCULABLE = "NOT_CALCULABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class QuarterlyInputs:
    cumulative_profit: Money
    received_dividends: Money
    fair_value_change: Money
    loss_carryforward: Money
    tax_rate: Rate
    prior_quarter_current_tax: Money
    current_quarter_current_tax: Money
    cumulative_revenue: Money
    historical_average_tax_burden: Rate
    other_payables_accrual: Money
    hesi_no_invoice: Money

    def __post_init__(self) -> None:
        money_fields = (
            "cumulative_profit",
            "received_dividends",
            "fair_value_change",
            "loss_carryforward",
            "prior_quarter_current_tax",
            "current_quarter_current_tax",
            "cumulative_revenue",
            "other_payables_accrual",
            "hesi_no_invoice",
        )
        reference: Money | None = None
        for field in money_fields:
            value = getattr(self, field)
            if not isinstance(value, Money):
                raise TypeError(f"{field} must be Money")
            if not _fits_database_amount(value.amount):
                raise ValueError(f"{field} must fit NUMERIC(38, 12)")
            if reference is None:
                reference = value
                continue
            if value.currency != reference.currency:
                raise ValueError("quarterly money inputs must use one currency")
            if value.scale != reference.scale:
                raise ValueError("quarterly money inputs must use one amount scale")
        assert reference is not None
        if re.fullmatch(r"[A-Z]{3}", reference.currency) is None:
            raise ValueError("quarterly input currency must use three uppercase letters")
        if reference.scale > 12:
            raise ValueError("quarterly input amount scale must not exceed 12")
        if not isinstance(self.tax_rate, Rate):
            raise TypeError("tax_rate must be Rate")
        if not isinstance(self.historical_average_tax_burden, Rate):
            raise TypeError("historical_average_tax_burden must be Rate")


FormulaValue: TypeAlias = Decimal | str | int | None


@dataclass(frozen=True, slots=True)
class QuarterlyResult:
    currency: str
    amount_scale: int
    rounding_mode: str
    accrual_status: CalculationStatus
    tax_burden_status: CalculationStatus
    potential_status: CalculationStatus
    base_before_floor: Money | None
    cumulative_base: Money | None
    cumulative_tax_payable: Money | None
    current_quarter_should_accrue: Money | None
    current_quarter_difference: Money | None
    accrual_alert_flag: bool
    accrual_alert_code: str | None
    current_tax_burden: Decimal | None
    tax_burden_deviation: Decimal | None
    tax_burden_alert_flag: bool
    tax_burden_alert_code: str | None
    potential_adjustment: Money | None
    potential_base: Money | None
    potential_tax_payable: Money | None
    potential_tax_cost: Money | None
    potential_tax_cost_alert_flag: bool
    potential_tax_cost_alert_code: str | None
    alerts: tuple[str, ...]
    accrual_not_calculated_reason: str | None
    tax_burden_not_calculated_reason: str | None
    potential_not_calculated_reason: str | None
    not_calculated_reason: str | None
    formula_substitution: Mapping[str, FormulaValue]


def calculate_quarterly(inputs: QuarterlyInputs) -> QuarterlyResult:
    """Calculate all three quarterly monitors from one frozen input bundle."""

    if not isinstance(inputs, QuarterlyInputs):
        raise TypeError("calculate_quarterly requires QuarterlyInputs")

    raw_base_before_floor = (
        inputs.cumulative_profit
        - inputs.received_dividends
        - inputs.fair_value_change
        - inputs.loss_carryforward
    )
    raw_cumulative_base = _floor_at_zero(raw_base_before_floor)
    raw_cumulative_tax = (raw_cumulative_base * inputs.tax_rate).quantized()
    raw_current_should_accrue = (
        raw_cumulative_tax - inputs.prior_quarter_current_tax
    ).quantized()
    raw_current_difference = (
        raw_current_should_accrue - inputs.current_quarter_current_tax
    ).quantized()
    raw_potential_adjustment = (
        inputs.other_payables_accrual + inputs.hesi_no_invoice
    )
    raw_potential_base = _floor_at_zero(
        raw_base_before_floor + raw_potential_adjustment
    )
    raw_potential_tax = (raw_potential_base * inputs.tax_rate).quantized()
    raw_potential_tax_cost = (
        raw_potential_tax - raw_cumulative_tax
    ).quantized()

    common_overflow = not _all_persistable(
        raw_base_before_floor,
        raw_cumulative_base,
        raw_cumulative_tax,
    )
    if common_overflow:
        accrual_status = CalculationStatus.FAILED
        accrual_reason = "AMOUNT_OVERFLOW"
        current_should_accrue = None
        current_difference = None
        accrual_alert_code = None
    else:
        accrual_overflow = not _all_persistable(
            raw_current_should_accrue,
            raw_current_difference,
        )
        if accrual_overflow:
            accrual_status = CalculationStatus.FAILED
            accrual_reason = "AMOUNT_OVERFLOW"
            current_should_accrue = None
            current_difference = None
            accrual_alert_code = None
        else:
            accrual_status = CalculationStatus.CALCULATED
            accrual_reason = None
            current_should_accrue = raw_current_should_accrue
            current_difference = raw_current_difference
            accrual_alert_code = _accrual_alert(raw_current_difference.amount)

    if common_overflow:
        current_tax_burden = None
        tax_burden_deviation = None
        tax_burden_status = CalculationStatus.FAILED
        tax_burden_alert_code = None
        tax_burden_reason = "AMOUNT_OVERFLOW"
    elif inputs.cumulative_revenue.amount <= Decimal("0"):
        current_tax_burden = None
        tax_burden_deviation = None
        tax_burden_status = CalculationStatus.NOT_CALCULABLE
        tax_burden_alert_code = None
        tax_burden_reason = "REVENUE_NON_POSITIVE"
    else:
        try:
            current_tax_burden, tax_burden_deviation = _tax_burden(
                raw_cumulative_tax.amount,
                inputs.cumulative_revenue.amount,
                inputs.historical_average_tax_burden.value,
            )
        except ArithmeticError:
            current_tax_burden = None
            tax_burden_deviation = None
            tax_burden_status = CalculationStatus.FAILED
            tax_burden_alert_code = None
            tax_burden_reason = "DECIMAL_CALCULATION_FAILED"
        else:
            tax_burden_status = CalculationStatus.CALCULATED
            tax_burden_alert_code = _tax_burden_alert(tax_burden_deviation)
            tax_burden_reason = None

    potential_overflow = common_overflow or not _all_persistable(
        raw_potential_adjustment,
        raw_potential_base,
        raw_potential_tax,
        raw_potential_tax_cost,
    )
    if potential_overflow:
        potential_status = CalculationStatus.FAILED
        potential_reason = "AMOUNT_OVERFLOW"
        potential_tax = None
        potential_tax_cost = None
        potential_alert_code = None
    else:
        potential_status = CalculationStatus.CALCULATED
        potential_reason = None
        potential_tax = raw_potential_tax
        potential_tax_cost = raw_potential_tax_cost
        potential_alert_code = (
            "POTENTIAL_TAX_COST"
            if raw_potential_tax_cost.amount != Decimal("0")
            else None
        )

    not_calculated_reason = _reason_summary(
        accrual_reason,
        tax_burden_reason,
        potential_reason,
    )

    alerts = tuple(
        code
        for code in (
            accrual_alert_code,
            tax_burden_alert_code,
            potential_alert_code,
        )
        if code is not None
    )
    formula_substitution: Mapping[str, FormulaValue] = MappingProxyType(
        {
            "currency": inputs.cumulative_profit.currency,
            "amount_scale": inputs.cumulative_profit.scale,
            "cumulative_profit": inputs.cumulative_profit.amount,
            "received_dividends": inputs.received_dividends.amount,
            "fair_value_change": inputs.fair_value_change.amount,
            "loss_carryforward": inputs.loss_carryforward.amount,
            "base_before_floor": raw_base_before_floor.amount,
            "cumulative_base": raw_cumulative_base.amount,
            "tax_rate": inputs.tax_rate.value,
            "rounding_mode": "ROUND_HALF_UP",
            "cumulative_tax_payable": raw_cumulative_tax.amount,
            "prior_quarter_current_tax": inputs.prior_quarter_current_tax.amount,
            "current_quarter_should_accrue": raw_current_should_accrue.amount,
            "current_quarter_current_tax": inputs.current_quarter_current_tax.amount,
            "current_quarter_difference": raw_current_difference.amount,
            "cumulative_revenue": inputs.cumulative_revenue.amount,
            "current_tax_burden": current_tax_burden,
            "historical_average_tax_burden": (
                inputs.historical_average_tax_burden.value
            ),
            "tax_burden_deviation": tax_burden_deviation,
            "other_payables_accrual": inputs.other_payables_accrual.amount,
            "hesi_no_invoice": inputs.hesi_no_invoice.amount,
            "potential_adjustment": raw_potential_adjustment.amount,
            "potential_base": raw_potential_base.amount,
            "potential_tax_payable": raw_potential_tax.amount,
            "potential_tax_cost": raw_potential_tax_cost.amount,
        }
    )

    return QuarterlyResult(
        currency=inputs.cumulative_profit.currency,
        amount_scale=inputs.cumulative_profit.scale,
        rounding_mode="ROUND_HALF_UP",
        accrual_status=accrual_status,
        tax_burden_status=tax_burden_status,
        potential_status=potential_status,
        base_before_floor=_persistable(raw_base_before_floor),
        cumulative_base=_persistable(raw_cumulative_base),
        cumulative_tax_payable=(
            None if common_overflow else raw_cumulative_tax
        ),
        current_quarter_should_accrue=current_should_accrue,
        current_quarter_difference=current_difference,
        accrual_alert_flag=accrual_alert_code is not None,
        accrual_alert_code=accrual_alert_code,
        current_tax_burden=current_tax_burden,
        tax_burden_deviation=tax_burden_deviation,
        tax_burden_alert_flag=tax_burden_alert_code is not None,
        tax_burden_alert_code=tax_burden_alert_code,
        potential_adjustment=_persistable(raw_potential_adjustment),
        potential_base=_persistable(raw_potential_base),
        potential_tax_payable=potential_tax,
        potential_tax_cost=potential_tax_cost,
        potential_tax_cost_alert_flag=potential_alert_code is not None,
        potential_tax_cost_alert_code=potential_alert_code,
        alerts=alerts,
        accrual_not_calculated_reason=accrual_reason,
        tax_burden_not_calculated_reason=tax_burden_reason,
        potential_not_calculated_reason=potential_reason,
        not_calculated_reason=not_calculated_reason,
        formula_substitution=formula_substitution,
    )


def _floor_at_zero(value: Money) -> Money:
    if value.amount > Decimal("0"):
        return value
    return Money.unrounded(Decimal("0"), currency=value.currency, scale=value.scale)


def _accrual_alert(difference: Decimal) -> str | None:
    if difference > Decimal("0"):
        return "UNDER_ACCRUED"
    if difference < Decimal("0"):
        return "OVER_ACCRUED"
    return None


def _tax_burden(
    cumulative_tax: Decimal,
    cumulative_revenue: Decimal,
    historical_average: Decimal,
) -> tuple[Decimal, Decimal]:
    # Construct a fresh context and explicitly clear every trap so caller changes
    # to getcontext()/DefaultContext cannot affect a deterministic rule result.
    context = Context(
        prec=96,
        rounding=ROUND_HALF_UP,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        clamp=0,
    )
    for signal in context.traps:
        context.traps[signal] = False
    burden = context.divide(cumulative_tax, cumulative_revenue)
    deviation = context.subtract(burden, historical_average)
    if not burden.is_finite() or not deviation.is_finite():
        raise ArithmeticError("tax burden calculation produced a non-finite result")
    return burden, deviation


def _tax_burden_alert(deviation: Decimal) -> str | None:
    threshold = Decimal("0.05")
    if deviation >= threshold:
        return "TAX_BURDEN_HIGH"
    if deviation <= -threshold:
        return "TAX_BURDEN_LOW"
    return None


def _fits_database_amount(amount: Decimal) -> bool:
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return False
    integral_digits = max(amount.adjusted() + 1, 0) if amount else 0
    exponent = cast(int, amount.as_tuple().exponent)
    fractional_digits = max(-exponent, 0)
    return integral_digits <= 26 and fractional_digits <= 12


def _all_persistable(*values: Money) -> bool:
    return all(_fits_database_amount(value.amount) for value in values)


def _persistable(value: Money) -> Money | None:
    return value if _fits_database_amount(value.amount) else None


def _reason_summary(*reasons: str | None) -> str | None:
    unique = tuple(dict.fromkeys(reason for reason in reasons if reason is not None))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return "MULTIPLE_MONITOR_REASONS"


__all__ = [
    "CalculationStatus",
    "QuarterlyInputs",
    "QuarterlyResult",
    "calculate_quarterly",
]

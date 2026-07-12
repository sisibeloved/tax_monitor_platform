from decimal import Decimal

from hypothesis import given, settings, strategies as st

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import QuarterlyInputs, calculate_quarterly


def _money(value: Decimal) -> Money:
    return Money.unrounded(value, currency="CNY", scale=2)


def _inputs(*, profit: Decimal, adjustment: Decimal) -> QuarterlyInputs:
    zero = _money(Decimal("0"))
    return QuarterlyInputs(
        cumulative_profit=_money(profit),
        received_dividends=zero,
        fair_value_change=zero,
        loss_carryforward=zero,
        tax_rate=Rate.from_fraction("0.25"),
        prior_quarter_current_tax=zero,
        current_quarter_current_tax=zero,
        cumulative_revenue=_money(Decimal("1000000")),
        historical_average_tax_burden=Rate.from_fraction("0"),
        other_payables_accrual=_money(adjustment),
        hesi_no_invoice=zero,
    )


finite_amounts = st.decimals(
    min_value=Decimal("-1000000"),
    max_value=Decimal("1000000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
nonnegative_amounts = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@settings(max_examples=100)
@given(profit=finite_amounts, adjustment=finite_amounts)
def test_identical_inputs_are_deterministic(profit: Decimal, adjustment: Decimal) -> None:
    inputs = _inputs(profit=profit, adjustment=adjustment)

    assert calculate_quarterly(inputs) == calculate_quarterly(inputs)


@settings(max_examples=100)
@given(profit=finite_amounts, adjustment=finite_amounts)
def test_cumulative_tax_is_always_nonnegative(
    profit: Decimal,
    adjustment: Decimal,
) -> None:
    result = calculate_quarterly(_inputs(profit=profit, adjustment=adjustment))

    assert result.cumulative_tax_payable.amount >= Decimal("0")


@settings(max_examples=100)
@given(
    profit=finite_amounts,
    smaller=nonnegative_amounts,
    increment=nonnegative_amounts,
)
def test_increasing_nonnegative_potential_adjustment_never_decreases_tax(
    profit: Decimal,
    smaller: Decimal,
    increment: Decimal,
) -> None:
    first = calculate_quarterly(_inputs(profit=profit, adjustment=smaller))
    second = calculate_quarterly(
        _inputs(profit=profit, adjustment=smaller + increment)
    )

    assert second.potential_tax_payable.amount >= first.potential_tax_payable.amount

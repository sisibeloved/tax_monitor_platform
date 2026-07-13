from dataclasses import FrozenInstanceError
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import (
    CalculationStatus,
    QuarterlyInputs,
    calculate_quarterly,
)


def _money(value: str, *, scale: int = 2) -> Money:
    return Money.unrounded(value, currency="CNY", scale=scale)


def _inputs(**overrides: object) -> QuarterlyInputs:
    values: dict[str, object] = {
        "cumulative_profit": _money("0"),
        "received_dividends": _money("0"),
        "fair_value_change": _money("0"),
        "loss_carryforward": _money("0"),
        "tax_rate": Rate.from_fraction("0.25"),
        "prior_quarter_current_tax": _money("0"),
        "current_quarter_current_tax": _money("0"),
        "cumulative_revenue": _money("100"),
        "historical_average_tax_burden": Rate.from_fraction("0"),
        "other_payables_accrual": _money("0"),
        "hesi_no_invoice": _money("0"),
    }
    values.update(overrides)
    return QuarterlyInputs(**values)  # type: ignore[arg-type]


def test_standard_quarterly_example() -> None:
    result = calculate_quarterly(
        QuarterlyInputs(
            cumulative_profit=_money("10000000"),
            received_dividends=_money("1000000"),
            fair_value_change=_money("500000"),
            loss_carryforward=_money("2000000"),
            tax_rate=Rate.from_fraction("0.25"),
            prior_quarter_current_tax=_money("900000"),
            current_quarter_current_tax=_money("700000"),
            cumulative_revenue=_money("50000000"),
            historical_average_tax_burden=Rate.from_fraction("0.09"),
            other_payables_accrual=_money("1400000"),
            hesi_no_invoice=_money("300000"),
        )
    )

    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.tax_burden_status is CalculationStatus.CALCULATED
    assert result.potential_status is CalculationStatus.CALCULATED
    assert result.currency == "CNY"
    assert result.amount_scale == 2
    assert result.rounding_mode == "ROUND_HALF_UP"
    assert result.base_before_floor.amount == Decimal("6500000")
    assert result.cumulative_base.amount == Decimal("6500000")
    assert result.cumulative_tax_payable.amount == Decimal("1625000.00")
    assert result.current_quarter_should_accrue.amount == Decimal("725000.00")
    assert result.current_quarter_difference.amount == Decimal("25000.00")
    assert result.accrual_alert_flag is True
    assert result.accrual_alert_code == "UNDER_ACCRUED"
    assert result.current_tax_burden == Decimal("0.0325")
    assert result.tax_burden_deviation == Decimal("-0.0575")
    assert result.tax_burden_alert_flag is True
    assert result.tax_burden_alert_code == "TAX_BURDEN_LOW"
    assert result.potential_adjustment.amount == Decimal("1700000")
    assert result.potential_base.amount == Decimal("8200000")
    assert result.potential_tax_payable.amount == Decimal("2050000.00")
    assert result.potential_tax_cost.amount == Decimal("425000.00")
    assert result.potential_tax_cost_alert_flag is True
    assert result.potential_tax_cost_alert_code == "POTENTIAL_TAX_COST"
    assert result.alerts == (
        "UNDER_ACCRUED",
        "TAX_BURDEN_LOW",
        "POTENTIAL_TAX_COST",
    )
    assert result.not_calculated_reason is None
    assert result.formula_substitution["base_before_floor"] == Decimal("6500000")
    assert result.formula_substitution["cumulative_base"] == Decimal("6500000")
    assert set(result.formula_substitution) == {
        "amount_scale",
        "base_before_floor",
        "cumulative_base",
        "cumulative_profit",
        "cumulative_revenue",
        "cumulative_tax_payable",
        "currency",
        "current_quarter_current_tax",
        "current_quarter_difference",
        "current_quarter_should_accrue",
        "current_tax_burden",
        "fair_value_change",
        "hesi_no_invoice",
        "historical_average_tax_burden",
        "loss_carryforward",
        "other_payables_accrual",
        "potential_adjustment",
        "potential_base",
        "potential_tax_cost",
        "potential_tax_payable",
        "prior_quarter_current_tax",
        "received_dividends",
        "rounding_mode",
        "tax_burden_deviation",
        "tax_rate",
    }


@pytest.mark.parametrize(
    ("adjustment", "expected_base", "expected_tax", "expected_cost", "alert"),
    [
        ("60", "0", "0.00", "0.00", None),
        ("150", "50", "12.50", "12.50", "POTENTIAL_TAX_COST"),
    ],
)
def test_potential_base_uses_pre_floor_base(
    adjustment: str,
    expected_base: str,
    expected_tax: str,
    expected_cost: str,
    alert: str | None,
) -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("-100"),
            other_payables_accrual=_money(adjustment),
        )
    )

    assert result.base_before_floor.amount == Decimal("-100")
    assert result.cumulative_base.amount == Decimal("0")
    assert result.potential_base.amount == Decimal(expected_base)
    assert result.potential_tax_payable.amount == Decimal(expected_tax)
    assert result.potential_tax_cost.amount == Decimal(expected_cost)
    assert result.potential_tax_cost_alert_code == alert


@pytest.mark.parametrize(
    ("overrides", "expected_base"),
    [
        ({"cumulative_profit": _money("-10")}, "0"),
        ({"cumulative_profit": _money("100"), "fair_value_change": _money("-20")}, "120"),
        ({"cumulative_profit": _money("100"), "loss_carryforward": _money("100")}, "0"),
        ({"cumulative_profit": _money("100"), "loss_carryforward": _money("60")}, "40"),
        ({"cumulative_profit": _money("100"), "received_dividends": _money("-20")}, "120"),
    ],
)
def test_signed_profit_dividend_fair_value_and_loss_entries_are_algebraic(
    overrides: dict[str, Money],
    expected_base: str,
) -> None:
    result = calculate_quarterly(_inputs(**overrides))

    assert result.cumulative_base.amount == Decimal(expected_base)


@pytest.mark.parametrize(
    ("actual", "expected_difference", "expected_code"),
    [
        ("20", "5.00", "UNDER_ACCRUED"),
        ("30", "-5.00", "OVER_ACCRUED"),
        ("25", "0.00", None),
        ("-0.00", "25.00", "UNDER_ACCRUED"),
    ],
)
def test_accrual_alert_uses_the_signed_final_difference(
    actual: str,
    expected_difference: str,
    expected_code: str | None,
) -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("100"),
            current_quarter_current_tax=_money(actual),
        )
    )

    assert result.current_quarter_difference.amount == Decimal(expected_difference)
    assert result.accrual_alert_code == expected_code
    assert result.accrual_alert_flag is (expected_code is not None)


def test_current_quarter_should_accrue_can_be_negative() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("40"),
            prior_quarter_current_tax=_money("15"),
            current_quarter_current_tax=_money("-2"),
        )
    )

    assert result.cumulative_tax_payable.amount == Decimal("10.00")
    assert result.current_quarter_should_accrue.amount == Decimal("-5.00")
    assert result.current_quarter_difference.amount == Decimal("-3.00")
    assert result.accrual_alert_code == "OVER_ACCRUED"


@pytest.mark.parametrize(
    ("profit", "historical", "expected_deviation", "expected_code"),
    [
        ("60", "0.10", "0.05", "TAX_BURDEN_HIGH"),
        ("20", "0.10", "-0.05", "TAX_BURDEN_LOW"),
        ("59.96", "0.10", "0.0499", None),
        ("20.04", "0.10", "-0.0499", None),
        ("0", "0", "0.00", None),
        ("-0.00", "0", "-0.00", None),
    ],
)
def test_tax_burden_alert_boundaries_include_exact_five_percent_and_signed_zero(
    profit: str,
    historical: str,
    expected_deviation: str,
    expected_code: str | None,
) -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money(profit),
            historical_average_tax_burden=Rate.from_fraction(historical),
        )
    )

    assert result.tax_burden_deviation == Decimal(expected_deviation)
    assert result.tax_burden_alert_code == expected_code
    assert result.tax_burden_alert_flag is (expected_code is not None)


@pytest.mark.parametrize("revenue", ["0", "-1", "-0.00"])
def test_nonpositive_revenue_only_makes_tax_burden_not_calculable(revenue: str) -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("100"),
            cumulative_revenue=_money(revenue),
            other_payables_accrual=_money("10"),
        )
    )

    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.tax_burden_status is CalculationStatus.NOT_CALCULABLE
    assert result.potential_status is CalculationStatus.CALCULATED
    assert result.current_tax_burden is None
    assert result.tax_burden_deviation is None
    assert result.tax_burden_alert_flag is False
    assert result.tax_burden_alert_code is None
    assert result.not_calculated_reason == "REVENUE_NON_POSITIVE"
    assert result.cumulative_tax_payable.amount == Decimal("25.00")
    assert result.potential_tax_payable.amount == Decimal("27.50")


def test_tax_burden_uses_the_already_rounded_cumulative_tax() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("1.005"),
            tax_rate=Rate.from_fraction("1"),
            cumulative_revenue=_money("2"),
        )
    )

    assert result.cumulative_tax_payable.amount == Decimal("1.01")
    assert result.current_tax_burden == Decimal("0.505")
    assert result.current_tax_burden != Decimal("0.5025")


@pytest.mark.parametrize(
    ("adjustment", "expected_cost", "expected_code"),
    [
        ("10", "2.50", "POTENTIAL_TAX_COST"),
        ("-10", "-2.50", "POTENTIAL_TAX_COST"),
        ("0", "0.00", None),
        ("-0.00", "0.00", None),
    ],
)
def test_potential_cost_alert_uses_nonzero_final_cost(
    adjustment: str,
    expected_cost: str,
    expected_code: str | None,
) -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money("100"),
            other_payables_accrual=_money(adjustment),
        )
    )

    assert result.potential_tax_cost.amount == Decimal(expected_cost)
    assert result.potential_tax_cost_alert_code == expected_code
    assert result.potential_tax_cost_alert_flag is (expected_code is not None)


def test_quarterly_inputs_result_and_formula_substitution_are_immutable() -> None:
    inputs = _inputs(cumulative_profit=_money("100"))
    result = calculate_quarterly(inputs)

    with pytest.raises(FrozenInstanceError):
        inputs.cumulative_profit = _money("200")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.currency = "USD"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.formula_substitution["base_before_floor"] = Decimal("0")  # type: ignore[index]


def test_failed_status_is_reserved_for_future_runtime_failures() -> None:
    assert CalculationStatus.FAILED.value == "FAILED"


def test_tax_burden_is_independent_from_hostile_ambient_decimal_context() -> None:
    inputs = _inputs(
        cumulative_profit=_money("1"),
        tax_rate=Rate.from_fraction("1"),
        cumulative_revenue=_money("3"),
    )
    expected = calculate_quarterly(inputs)

    with localcontext() as hostile:
        hostile.prec = 2
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        hostile.traps[Rounded] = True
        actual = calculate_quarterly(inputs)

    assert actual == expected
    assert actual.tax_burden_status is CalculationStatus.CALCULATED


PERSISTED_MAX = Decimal("99999999999999999999999999.00")


def test_shared_cumulative_tax_overflow_fails_all_dependent_monitors() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money(str(PERSISTED_MAX)),
            received_dividends=_money(str(-PERSISTED_MAX)),
            fair_value_change=_money(str(-PERSISTED_MAX)),
            loss_carryforward=_money(str(PERSISTED_MAX)),
            tax_rate=Rate.from_fraction("1"),
            cumulative_revenue=_money(str(PERSISTED_MAX)),
        )
    )

    assert result.accrual_status is CalculationStatus.FAILED
    assert result.tax_burden_status is CalculationStatus.FAILED
    assert result.potential_status is CalculationStatus.FAILED
    assert result.cumulative_tax_payable is None
    assert result.current_quarter_should_accrue is None
    assert result.current_quarter_difference is None
    assert result.current_tax_burden is None
    assert result.tax_burden_deviation is None
    assert result.potential_tax_payable is None
    assert result.potential_tax_cost is None
    assert result.accrual_not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.tax_burden_not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.potential_not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.alerts == ()
    assert result.formula_substitution["cumulative_tax_payable"] == (
        PERSISTED_MAX * 2
    )


def test_accrual_only_overflow_does_not_hide_other_monitor_results() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money(str(PERSISTED_MAX)),
            tax_rate=Rate.from_fraction("1"),
            prior_quarter_current_tax=_money(str(-PERSISTED_MAX)),
            cumulative_revenue=_money(str(PERSISTED_MAX)),
        )
    )

    assert result.accrual_status is CalculationStatus.FAILED
    assert result.tax_burden_status is CalculationStatus.CALCULATED
    assert result.potential_status is CalculationStatus.CALCULATED
    assert result.cumulative_tax_payable is not None
    assert result.current_quarter_should_accrue is None
    assert result.current_quarter_difference is None
    assert result.current_tax_burden == Decimal("1")
    assert result.potential_tax_cost is not None
    assert result.potential_tax_cost.amount == Decimal("0.00")
    assert result.accrual_alert_flag is False
    assert result.accrual_alert_code is None
    assert result.accrual_not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.tax_burden_not_calculated_reason is None
    assert result.potential_not_calculated_reason is None


def test_potential_only_overflow_does_not_hide_other_monitor_results() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money(str(PERSISTED_MAX)),
            tax_rate=Rate.from_fraction("1"),
            cumulative_revenue=_money(str(PERSISTED_MAX)),
            other_payables_accrual=_money(str(PERSISTED_MAX)),
        )
    )

    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.tax_burden_status is CalculationStatus.CALCULATED
    assert result.potential_status is CalculationStatus.FAILED
    assert result.current_quarter_difference is not None
    assert result.current_tax_burden == Decimal("1")
    assert result.potential_base is None
    assert result.potential_tax_payable is None
    assert result.potential_tax_cost is None
    assert result.potential_tax_cost_alert_flag is False
    assert result.potential_tax_cost_alert_code is None
    assert result.accrual_not_calculated_reason is None
    assert result.tax_burden_not_calculated_reason is None
    assert result.potential_not_calculated_reason == "AMOUNT_OVERFLOW"


def test_monitor_specific_reasons_preserve_simultaneous_distinct_failures() -> None:
    result = calculate_quarterly(
        _inputs(
            cumulative_profit=_money(str(PERSISTED_MAX)),
            tax_rate=Rate.from_fraction("1"),
            cumulative_revenue=_money("0"),
            other_payables_accrual=_money(str(PERSISTED_MAX)),
        )
    )

    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.tax_burden_status is CalculationStatus.NOT_CALCULABLE
    assert result.potential_status is CalculationStatus.FAILED
    assert result.accrual_not_calculated_reason is None
    assert result.tax_burden_not_calculated_reason == "REVENUE_NON_POSITIVE"
    assert result.potential_not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.not_calculated_reason == "MULTIPLE_MONITOR_REASONS"

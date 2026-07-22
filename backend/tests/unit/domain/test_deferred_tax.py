from dataclasses import FrozenInstanceError
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import (
    CalculationStatus,
    DeferredTaxBaseFormula,
    DeferredTaxInputs,
    calculate_deferred_tax,
)


def _money(
    value: str = "0",
    *,
    currency: str = "CNY",
    scale: int = 2,
) -> Money:
    return Money.unrounded(value, currency=currency, scale=scale)


def _inputs(**overrides: object) -> DeferredTaxInputs:
    values: dict[str, object] = {
        "cumulative_profit": _money(),
        "loss_carryforward": _money(),
        "deferred_tax_rate": Rate.from_fraction("0.25"),
        "sap_cumulative_deferred_tax_expense": _money(),
    }
    values.update(overrides)
    return DeferredTaxInputs(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    (
        "profit",
        "loss",
        "sap_expense",
        "expected_base",
        "expected_system_tax",
        "expected_adjustment",
        "expected_code",
    ),
    [
        ("20", "100", "15", "80", "20.00", "5.00", "DEFERRED_TAX_TO_ACCRUE"),
        ("20", "100", "30", "80", "20.00", "-10.00", "DEFERRED_TAX_TO_REVERSE"),
        ("120", "100", "-5", "-20", "-5.00", "0.00", None),
    ],
)
def test_deferred_tax_subtracts_profit_without_flooring_the_base(
    profit: str,
    loss: str,
    sap_expense: str,
    expected_base: str,
    expected_system_tax: str,
    expected_adjustment: str,
    expected_code: str | None,
) -> None:
    result = calculate_deferred_tax(
        _inputs(
            cumulative_profit=_money(profit),
            loss_carryforward=_money(loss),
            sap_cumulative_deferred_tax_expense=_money(sap_expense),
        )
    )

    assert result.status is CalculationStatus.CALCULATED
    assert result.deferred_tax_base is not None
    assert result.system_cumulative_deferred_tax is not None
    assert result.current_year_deferred_tax_adjustment is not None
    assert result.deferred_tax_base.amount == Decimal(expected_base)
    assert result.system_cumulative_deferred_tax.amount == Decimal(
        expected_system_tax
    )
    assert result.current_year_deferred_tax_adjustment.amount == Decimal(
        expected_adjustment
    )
    assert result.alert_flag is (expected_code is not None)
    assert result.alert_code == expected_code
    assert result.not_calculated_reason is None


def test_final_quantized_zero_does_not_alert() -> None:
    result = calculate_deferred_tax(
        _inputs(
            cumulative_profit=_money("0.01"),
            sap_cumulative_deferred_tax_expense=_money("0.004"),
        )
    )

    assert result.system_cumulative_deferred_tax is not None
    assert result.current_year_deferred_tax_adjustment is not None
    assert result.system_cumulative_deferred_tax.amount == Decimal("0.00")
    assert result.current_year_deferred_tax_adjustment.amount == Decimal("-0.00")
    assert result.alert_flag is False
    assert result.alert_code is None


def test_formula_substitution_contains_inputs_and_outputs() -> None:
    result = calculate_deferred_tax(
        _inputs(
            cumulative_profit=_money("60"),
            loss_carryforward=_money("40"),
            sap_cumulative_deferred_tax_expense=_money("20"),
        )
    )

    assert result.currency == "CNY"
    assert result.amount_scale == 2
    assert result.rounding_mode == "ROUND_HALF_UP"
    assert dict(result.formula_substitution) == {
        "currency": "CNY",
        "amount_scale": 2,
        "cumulative_profit": Decimal("60"),
        "loss_carryforward": Decimal("40"),
        "deferred_tax_rate": Decimal("0.25"),
        "sap_cumulative_deferred_tax_expense": Decimal("20"),
        "deferred_tax_base_formula": "LOSS_MINUS_PROFIT",
        "deferred_tax_base": Decimal("-20"),
        "system_cumulative_deferred_tax": Decimal("-5.00"),
        "current_year_deferred_tax_adjustment": Decimal("-25.00"),
        "rounding_mode": "ROUND_HALF_UP",
    }


@pytest.mark.parametrize("loss", ["-1", "-0.001"])
def test_loss_carryforward_must_be_nonnegative(loss: str) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _inputs(loss_carryforward=_money(loss))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cumulative_profit", Decimal("1"), "Money"),
        ("deferred_tax_rate", Decimal("0.25"), "Rate"),
        ("loss_carryforward", _money("1", currency="USD"), "currency"),
        ("sap_cumulative_deferred_tax_expense", _money("1", scale=3), "scale"),
        ("cumulative_profit", _money("100000000000000000000000000"), "NUMERIC"),
        ("cumulative_profit", _money("0.0000000000001"), "NUMERIC"),
        ("cumulative_profit", _money("1", scale=13), "amount scale"),
        ("cumulative_profit", _money("1", currency="CNYY"), "currency"),
    ],
)
def test_inputs_enforce_existing_money_rate_and_persistence_rules(
    field: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "cumulative_profit": _money(),
        "loss_carryforward": _money(),
        "deferred_tax_rate": Rate.from_fraction("0.25"),
        "sap_cumulative_deferred_tax_expense": _money(),
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        DeferredTaxInputs(**values)  # type: ignore[arg-type]


def test_calculator_rejects_the_wrong_input_bundle() -> None:
    with pytest.raises(TypeError, match="DeferredTaxInputs"):
        calculate_deferred_tax(object())  # type: ignore[arg-type]


def test_legacy_v2_formula_remains_available_for_historical_replay() -> None:
    result = calculate_deferred_tax(
        _inputs(
            cumulative_profit=_money("60"),
            loss_carryforward=_money("40"),
            sap_cumulative_deferred_tax_expense=_money("20"),
        ),
        base_formula=DeferredTaxBaseFormula.LOSS_PLUS_PROFIT,
    )

    assert result.deferred_tax_base is not None
    assert result.current_year_deferred_tax_adjustment is not None
    assert result.deferred_tax_base.amount == Decimal("100")
    assert result.current_year_deferred_tax_adjustment.amount == Decimal("5.00")
    assert result.formula_substitution["deferred_tax_base_formula"] == (
        "LOSS_PLUS_PROFIT"
    )


def test_inputs_result_and_formula_substitution_are_immutable() -> None:
    inputs = _inputs(cumulative_profit=_money("100"))
    result = calculate_deferred_tax(inputs)

    with pytest.raises(FrozenInstanceError):
        inputs.cumulative_profit = _money("200")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.currency = "USD"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.formula_substitution["deferred_tax_base"] = Decimal("0")  # type: ignore[index]


def test_derived_amount_overflow_returns_failed_without_alert() -> None:
    maximum = "99999999999999999999999999.00"
    result = calculate_deferred_tax(
        _inputs(
            cumulative_profit=_money(f"-{maximum}"),
            loss_carryforward=_money(maximum),
            deferred_tax_rate=Rate.from_fraction("1"),
        )
    )

    assert result.status is CalculationStatus.FAILED
    assert result.deferred_tax_base is None
    assert result.system_cumulative_deferred_tax is None
    assert result.current_year_deferred_tax_adjustment is None
    assert result.alert_flag is False
    assert result.alert_code is None
    assert result.not_calculated_reason == "AMOUNT_OVERFLOW"
    assert result.formula_substitution["deferred_tax_base"] == Decimal(maximum) * 2


def test_adjustment_overflow_preserves_persistable_intermediate_values() -> None:
    maximum = "99999999999999999999999999.00"
    result = calculate_deferred_tax(
        _inputs(
            loss_carryforward=_money(maximum),
            deferred_tax_rate=Rate.from_fraction("1"),
            sap_cumulative_deferred_tax_expense=_money(f"-{maximum}"),
        )
    )

    assert result.status is CalculationStatus.FAILED
    assert result.deferred_tax_base is not None
    assert result.system_cumulative_deferred_tax is not None
    assert result.current_year_deferred_tax_adjustment is None
    assert result.alert_flag is False
    assert result.alert_code is None
    assert result.not_calculated_reason == "AMOUNT_OVERFLOW"


def test_calculation_is_independent_from_hostile_ambient_decimal_context() -> None:
    inputs = _inputs(
        cumulative_profit=_money("1"),
        deferred_tax_rate=Rate.from_fraction("0.333333333333"),
    )
    expected = calculate_deferred_tax(inputs)

    with localcontext() as hostile:
        hostile.prec = 2
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        hostile.traps[Rounded] = True
        actual = calculate_deferred_tax(inputs)

    assert actual == expected

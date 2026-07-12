from decimal import Decimal
from typing import Any

import pytest

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import (
    CalculationStatus,
    QuarterlyInputs,
    calculate_quarterly,
)


MONEY_FIELDS = (
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


def _money(
    value: str = "0",
    *,
    currency: str = "CNY",
    scale: int = 2,
) -> Money:
    return Money.unrounded(value, currency=currency, scale=scale)


def _valid_values() -> dict[str, object]:
    return {
        "cumulative_profit": _money(),
        "received_dividends": _money(),
        "fair_value_change": _money(),
        "loss_carryforward": _money(),
        "tax_rate": Rate.from_fraction("0.25"),
        "prior_quarter_current_tax": _money(),
        "current_quarter_current_tax": _money(),
        "cumulative_revenue": _money("100"),
        "historical_average_tax_burden": Rate.from_fraction("0.10"),
        "other_payables_accrual": _money(),
        "hesi_no_invoice": _money(),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("received_dividends", _money(currency="USD"), "currency"),
        ("fair_value_change", _money(scale=3), "scale"),
        ("cumulative_profit", Decimal("1"), "Money"),
        ("tax_rate", Decimal("0.25"), "Rate"),
        ("historical_average_tax_burden", None, "Rate"),
    ],
)
def test_inputs_reject_incompatible_currency_scale_and_types(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _valid_values()
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        QuarterlyInputs(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_money_input_is_rejected(amount: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        values = _valid_values()
        values["cumulative_profit"] = _money(amount)
        QuarterlyInputs(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_rate_input_is_rejected(rate: str) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        values = _valid_values()
        values["tax_rate"] = Rate.from_fraction(rate)
        QuarterlyInputs(**values)  # type: ignore[arg-type]


def test_calculator_rejects_non_quarterly_input() -> None:
    with pytest.raises(TypeError, match="QuarterlyInputs"):
        calculate_quarterly(Any)  # type: ignore[arg-type]


def test_historical_average_is_only_accepted_as_master_provided_rate() -> None:
    values = _valid_values()
    values["historical_average_tax_burden"] = _money("0.10")

    with pytest.raises(TypeError, match="Rate"):
        QuarterlyInputs(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "cumulative_profit",
            _money("100000000000000000000000000"),
            "NUMERIC",
        ),
        ("cumulative_profit", _money("0.0000000000001"), "NUMERIC"),
        ("cumulative_profit", _money("1", scale=13), "amount scale"),
        ("cumulative_profit", _money("1", currency="CNYY"), "currency"),
    ],
)
def test_inputs_reject_values_that_cannot_cross_the_persistence_boundary(
    field: str,
    value: Money,
    message: str,
) -> None:
    values = _valid_values()
    values[field] = value

    with pytest.raises(ValueError, match=message):
        QuarterlyInputs(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("money_kwargs", "message"),
    [
        ({"currency": "CNYY"}, "three uppercase letters"),
        ({"scale": 13}, "scale must not exceed 12"),
    ],
)
def test_inputs_reject_consistently_invalid_money_metadata(
    money_kwargs: dict[str, object],
    message: str,
) -> None:
    values = _valid_values()
    for field in MONEY_FIELDS:
        values[field] = _money(**money_kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        QuarterlyInputs(**values)  # type: ignore[arg-type]


def test_inputs_reject_a_money_value_corrupted_after_construction() -> None:
    corrupted_money = _money()
    # Exercise QuarterlyInputs' own persistence-boundary defence. This cannot be
    # constructed through Money's public API, but may arrive from unsafe hydration.
    object.__setattr__(corrupted_money, "amount", Decimal("NaN"))
    values = _valid_values()
    values["cumulative_profit"] = corrupted_money

    with pytest.raises(ValueError, match="NUMERIC"):
        QuarterlyInputs(**values)  # type: ignore[arg-type]


def test_nonfinite_tax_burden_result_fails_only_that_monitor() -> None:
    corrupted_historical_average = Rate.from_fraction("0.10")
    # Model a damaged master-data object hydrated without Rate.__init__. The
    # calculator must contain the Decimal failure to the tax-burden monitor.
    object.__setattr__(
        corrupted_historical_average,
        "value",
        Decimal("NaN"),
    )
    values = _valid_values()
    values["historical_average_tax_burden"] = corrupted_historical_average
    values["cumulative_profit"] = _money("100")

    result = calculate_quarterly(QuarterlyInputs(**values))  # type: ignore[arg-type]

    assert result.accrual_status is CalculationStatus.CALCULATED
    assert result.tax_burden_status is CalculationStatus.FAILED
    assert result.potential_status is CalculationStatus.CALCULATED
    assert result.current_tax_burden is None
    assert result.tax_burden_deviation is None
    assert result.tax_burden_alert_flag is False
    assert result.tax_burden_alert_code is None
    assert result.tax_burden_not_calculated_reason == "DECIMAL_CALCULATION_FAILED"
    assert result.not_calculated_reason == "DECIMAL_CALCULATION_FAILED"

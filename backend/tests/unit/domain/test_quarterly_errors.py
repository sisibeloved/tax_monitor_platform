from decimal import Decimal
from typing import Any

import pytest

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import QuarterlyInputs, calculate_quarterly


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

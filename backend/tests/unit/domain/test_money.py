from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tax_risk.domain.money import Money, Rate


def test_money_retains_unrounded_decimal_and_normalizes_currency() -> None:
    money = Money.unrounded("1625000.005", currency=" cny ", scale=2)

    assert money.amount == Decimal("1625000.005")
    assert money.currency == "CNY"
    assert money.scale == 2


def test_money_rounds_half_up_only_when_quantized() -> None:
    raw = Money.unrounded("1625000.005", currency="CNY", scale=2)

    assert raw.amount == Decimal("1625000.005")
    assert raw.quantized().amount == Decimal("1625000.01")
    assert raw.amount == Decimal("1625000.005")


def test_money_rejects_binary_float() -> None:
    with pytest.raises(TypeError, match="Decimal-compatible string"):
        Money.unrounded(0.1, currency="CNY", scale=2)


@pytest.mark.parametrize("value", [1, True, None])
def test_money_rejects_other_non_decimal_inputs(value: object) -> None:
    with pytest.raises(TypeError, match="Decimal-compatible string"):
        Money.unrounded(value, currency="CNY", scale=2)


def test_money_rejects_invalid_currency() -> None:
    with pytest.raises(ValueError, match="currency"):
        Money.unrounded("1.00", currency="   ", scale=2)


def test_money_rejects_non_string_currency() -> None:
    with pytest.raises(TypeError, match="currency"):
        Money.unrounded("1.00", currency=b"cny", scale=2)  # type: ignore[arg-type]


def test_money_rejects_negative_scale() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Money.unrounded("1.00", currency="CNY", scale=-1)


@pytest.mark.parametrize("scale", [True, 2.0, Decimal("2")])
def test_money_rejects_non_integer_scale(scale: object) -> None:
    with pytest.raises(TypeError, match="integer"):
        Money.unrounded("1.00", currency="CNY", scale=scale)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_money_rejects_non_finite_amount(amount: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        Money.unrounded(amount, currency="CNY", scale=2)


def test_money_is_immutable() -> None:
    money = Money.unrounded("1.00", currency="CNY", scale=2)

    with pytest.raises(FrozenInstanceError):
        money.amount = Decimal("2.00")  # type: ignore[misc]


def test_money_addition_and_subtraction_remain_unrounded() -> None:
    left = Money.unrounded("1.005", currency="CNY", scale=2)
    right = Money.unrounded(Decimal("2.006"), currency="CNY", scale=2)

    total = left + right

    assert total.amount == Decimal("3.011")
    assert (total - right).amount == left.amount
    assert left.amount == Decimal("1.005")
    assert right.amount == Decimal("2.006")


@pytest.mark.parametrize(
    ("other", "message"),
    [
        (Money.unrounded("1.00", currency="USD", scale=2), "currency"),
        (Money.unrounded("1.00", currency="CNY", scale=3), "scale"),
    ],
)
@pytest.mark.parametrize("operation", ["add", "subtract"])
def test_money_arithmetic_rejects_currency_or_scale_mismatch(
    other: Money,
    message: str,
    operation: str,
) -> None:
    money = Money.unrounded("1.00", currency="CNY", scale=2)

    with pytest.raises(ValueError, match=message):
        if operation == "add":
            money + other
        else:
            money - other


@pytest.mark.parametrize("operation", ["add", "subtract"])
def test_money_arithmetic_rejects_non_money_operand(operation: str) -> None:
    money = Money.unrounded("1.00", currency="CNY", scale=2)

    with pytest.raises(TypeError, match="Money"):
        if operation == "add":
            money + 1  # type: ignore[operator]
        else:
            money - 1  # type: ignore[operator]


def test_rate_multiplication_keeps_the_full_unrounded_amount() -> None:
    money = Money.unrounded("10.005", currency="CNY", scale=2)
    rate = Rate.from_fraction("0.25")

    result = money * rate

    assert result.amount == Decimal("2.50125")
    assert result.currency == "CNY"
    assert result.scale == 2

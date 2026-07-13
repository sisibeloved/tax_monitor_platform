from decimal import Context, ROUND_DOWN, ROUND_HALF_UP, Decimal, Inexact, Rounded, localcontext

import pytest
from hypothesis import given, settings, strategies as st

from tax_risk.domain.money import Money, Rate


def _exact_scaled_decimal(coefficient: int, *, scale: int) -> Decimal:
    sign = int(coefficient < 0)
    digits = tuple(int(digit) for digit in str(abs(coefficient)))
    return Decimal((sign, digits, -scale))


def test_rate_stores_fraction_not_percent_number() -> None:
    assert Rate.from_fraction("0.25").value == Decimal("0.25")

    with pytest.raises(ValueError, match="between 0 and 1"):
        Rate.from_fraction("25")


def test_rate_accepts_decimal_and_inclusive_boundaries() -> None:
    assert Rate.from_fraction(Decimal("0")).value == Decimal("0")
    assert Rate.from_fraction(Decimal("1")).value == Decimal("1")


def test_rate_rejects_binary_float() -> None:
    with pytest.raises(TypeError, match="Decimal-compatible string"):
        Rate.from_fraction(0.25)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rate_rejects_non_finite_values(value: str) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        Rate.from_fraction(value)


def test_high_precision_operations_ignore_ambient_decimal_context() -> None:
    amount = Decimal("12345678901234567890123456.123456")
    increment = Decimal("0.000001")
    fraction = Decimal("0.123456789012")

    with localcontext() as hostile_context:
        hostile_context.prec = 4
        hostile_context.rounding = ROUND_DOWN
        hostile_context.traps[Inexact] = True
        hostile_context.traps[Rounded] = True
        money = Money.unrounded(amount, currency="CNY", scale=12)
        delta = Money.unrounded(increment, currency="CNY", scale=12)
        actual_sum = money + delta
        actual_difference = money - delta
        actual_product = money * Rate.from_fraction(fraction)

    assert actual_sum.amount == Decimal("12345678901234567890123456.123457")
    assert actual_difference.amount == Decimal("12345678901234567890123456.123455")
    assert actual_product.amount == Decimal(
        "1524157875319616034331961.521265746816265472"
    )


def test_quantization_ignores_ambient_precision_and_rounding_traps() -> None:
    amount = Decimal("12345678901234567890123456.125")

    with localcontext() as hostile_context:
        hostile_context.prec = 4
        hostile_context.rounding = ROUND_DOWN
        hostile_context.traps[Inexact] = True
        hostile_context.traps[Rounded] = True
        result = Money.unrounded(amount, currency="CNY", scale=2).quantized()

    assert result.amount == Decimal("12345678901234567890123456.13")


@settings(max_examples=125)
@given(
    amount=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
    scale=st.integers(min_value=0, max_value=6),
)
def test_quantization_is_deterministic_and_does_not_mutate_input(
    amount: Decimal,
    scale: int,
) -> None:
    raw = Money.unrounded(amount, currency="CNY", scale=scale)
    quantum = Decimal(1).scaleb(-scale)
    expected = amount.quantize(quantum, rounding=ROUND_HALF_UP)

    first = raw.quantized()
    second = raw.quantized()

    assert first == second
    assert first.amount == expected
    assert first.quantized() == first
    assert raw.amount == amount


@settings(max_examples=125)
@given(
    left=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
    right=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_unrounded_arithmetic_is_exact_and_reversible(left: Decimal, right: Decimal) -> None:
    left_money = Money.unrounded(left, currency="CNY", scale=2)
    right_money = Money.unrounded(right, currency="CNY", scale=2)

    total = left_money + right_money

    assert total.amount == left + right
    assert (total - right_money).amount == left
    assert left_money.amount == left
    assert right_money.amount == right


@settings(max_examples=125)
@given(fraction_units=st.integers(min_value=0, max_value=10_000))
def test_rate_accepts_every_fraction_in_the_closed_unit_interval(fraction_units: int) -> None:
    fraction = Decimal(fraction_units) / Decimal("10000")

    assert Rate.from_fraction(fraction).value == fraction


@settings(max_examples=125)
@given(
    fraction_units=st.one_of(
        st.integers(min_value=-100_000, max_value=-1),
        st.integers(min_value=10_001, max_value=100_000),
    )
)
def test_rate_rejects_every_fraction_outside_the_closed_unit_interval(
    fraction_units: int,
) -> None:
    fraction = Decimal(fraction_units) / Decimal("10000")

    with pytest.raises(ValueError, match="between 0 and 1"):
        Rate.from_fraction(fraction)


def test_exact_scaled_decimal_preserves_a_38_digit_coefficient() -> None:
    coefficient = 12345678901234567890123456789012345678

    amount = _exact_scaled_decimal(coefficient, scale=12)

    assert amount == Decimal("12345678901234567890123456.789012345678")
    assert amount.as_tuple().digits == tuple(int(digit) for digit in str(coefficient))
    assert amount.as_tuple().exponent == -12


@settings(max_examples=125)
@given(
    coefficient=st.integers(min_value=-(10**38 - 1), max_value=10**38 - 1),
    amount_scale=st.integers(min_value=0, max_value=12),
    fraction_units=st.integers(min_value=0, max_value=10**12),
)
def test_rate_multiplication_is_exact_under_low_ambient_precision(
    coefficient: int,
    amount_scale: int,
    fraction_units: int,
) -> None:
    amount = _exact_scaled_decimal(coefficient, scale=amount_scale)
    fraction = _exact_scaled_decimal(fraction_units, scale=12)

    assert amount.as_tuple().digits == tuple(
        int(digit) for digit in str(abs(coefficient))
    )
    assert amount.as_tuple().sign == int(coefficient < 0)
    assert amount.as_tuple().exponent == -amount_scale
    assert fraction.as_tuple().digits == tuple(
        int(digit) for digit in str(fraction_units)
    )
    assert fraction.as_tuple().exponent == -12

    reference_context = Context(prec=100)
    expected = reference_context.multiply(amount, fraction)

    with localcontext() as hostile_context:
        hostile_context.prec = 6
        actual = (
            Money.unrounded(amount, currency="CNY", scale=amount_scale)
            * Rate.from_fraction(fraction)
        ).amount

    assert actual == expected


@settings(max_examples=125)
@given(
    minor_units=st.integers(min_value=0, max_value=100_000_000),
    sign=st.sampled_from((-1, 1)),
)
def test_half_up_ties_round_away_from_zero(minor_units: int, sign: int) -> None:
    magnitude = Decimal(minor_units) / Decimal("100") + Decimal("0.005")
    amount = Decimal(sign) * magnitude
    expected = Decimal(sign) * Decimal(minor_units + 1) / Decimal("100")

    result = Money.unrounded(amount, currency="CNY", scale=2).quantized()

    assert result.amount == expected

"""Exact, immutable money and fractional-rate value objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Rounded,
)
from typing import TypeAlias

DecimalInput: TypeAlias = Decimal | str


def _to_decimal(value: DecimalInput, *, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueError(
                f"{field_name} must be a valid Decimal-compatible string"
            ) from error
    raise TypeError(f"{field_name} must be a Decimal or Decimal-compatible string")


def _normalize_currency(currency: str) -> str:
    if not isinstance(currency, str):
        raise TypeError("currency must be a string")
    normalized = currency.strip().upper()
    if not normalized:
        raise ValueError("currency must be a non-empty identifier")
    return normalized


def _validate_scale(scale: int) -> int:
    if type(scale) is not int:
        raise TypeError("scale must be an integer")
    if scale < 0:
        raise ValueError("scale must be non-negative")
    return scale


def _exact_context(precision: int) -> Context:
    context = Context(
        prec=max(1, precision),
        rounding=ROUND_HALF_UP,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        clamp=0,
    )
    context.traps[Inexact] = False
    context.traps[Rounded] = False
    return context


def _addition_precision(left: Decimal, right: Decimal) -> int:
    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    left_exponent = int(left_tuple.exponent)
    right_exponent = int(right_tuple.exponent)
    common_exponent = min(left_exponent, right_exponent)
    left_digits = len(left_tuple.digits) + left_exponent - common_exponent
    right_digits = len(right_tuple.digits) + right_exponent - common_exponent
    return max(left_digits, right_digits) + 1


def _multiplication_precision(left: Decimal, right: Decimal) -> int:
    return len(left.as_tuple().digits) + len(right.as_tuple().digits)


def _quantization_precision(value: Decimal, target_exponent: int) -> int:
    value_tuple = value.as_tuple()
    value_exponent = int(value_tuple.exponent)
    appended_zeros = max(0, value_exponent - target_exponent)
    return len(value_tuple.digits) + appended_zeros + 1


@dataclass(frozen=True, slots=True, init=False)
class Money:
    """An unrounded monetary amount with explicit currency and output scale."""

    amount: Decimal
    currency: str
    scale: int

    def __init__(self, amount: DecimalInput, *, currency: str, scale: int) -> None:
        decimal_amount = _to_decimal(amount, field_name="amount")
        if not decimal_amount.is_finite():
            raise ValueError("amount must be finite")
        object.__setattr__(self, "amount", decimal_amount)
        object.__setattr__(self, "currency", _normalize_currency(currency))
        object.__setattr__(self, "scale", _validate_scale(scale))

    @classmethod
    def unrounded(cls, amount: DecimalInput, *, currency: str, scale: int) -> Money:
        return cls(amount, currency=currency, scale=scale)

    def quantized(self) -> Money:
        target_exponent = -self.scale
        quantum = Decimal((0, (1,), target_exponent))
        context = _exact_context(_quantization_precision(self.amount, target_exponent))
        amount = context.quantize(self.amount, quantum)
        return Money.unrounded(amount, currency=self.currency, scale=self.scale)

    def _require_compatible(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError("money arithmetic requires a Money operand")
        if self.currency != other.currency:
            raise ValueError("money arithmetic requires matching currency")
        if self.scale != other.scale:
            raise ValueError("money arithmetic requires matching scale")
        return other

    def __add__(self, other: object) -> Money:
        compatible = self._require_compatible(other)
        context = _exact_context(_addition_precision(self.amount, compatible.amount))
        return Money.unrounded(
            context.add(self.amount, compatible.amount),
            currency=self.currency,
            scale=self.scale,
        )

    def __sub__(self, other: object) -> Money:
        compatible = self._require_compatible(other)
        context = _exact_context(_addition_precision(self.amount, compatible.amount))
        return Money.unrounded(
            context.subtract(self.amount, compatible.amount),
            currency=self.currency,
            scale=self.scale,
        )

    def __mul__(self, rate: Rate) -> Money:
        if not isinstance(rate, Rate):
            raise TypeError("Money can only be multiplied by Rate")
        context = _exact_context(_multiplication_precision(self.amount, rate.value))
        return Money.unrounded(
            context.multiply(self.amount, rate.value),
            currency=self.currency,
            scale=self.scale,
        )


@dataclass(frozen=True, slots=True, init=False)
class Rate:
    """A fractional rate stored in the inclusive interval from zero to one."""

    value: Decimal

    def __init__(self, value: DecimalInput) -> None:
        fraction = _to_decimal(value, field_name="rate")
        if not fraction.is_finite() or not Decimal("0") <= fraction <= Decimal("1"):
            raise ValueError("rate must be between 0 and 1 inclusive")
        object.__setattr__(self, "value", fraction)

    @classmethod
    def from_fraction(cls, value: DecimalInput) -> Rate:
        return cls(value)

"""Pure income-tax refund receipt and booking-account evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import re

from tax_risk.domain.money import Money


WRONG_ACCOUNT_ALERT_CODE = "REFUND_BOOKED_TO_WRONG_ACCOUNT"
AMBIGUOUS_MATCH_ALERT_CODE = "AMBIGUOUS_REFUND_MATCH"


class RefundAccountFamily(StrEnum):
    """Controlled SAP account families scanned for an income-tax refund."""

    INCOME_TAX_EXPENSE = "INCOME_TAX_EXPENSE"
    OTHER_INCOME = "OTHER_INCOME"
    TAXES_PAYABLE = "TAXES_PAYABLE"


class RefundMatchStage(StrEnum):
    """The ordered account-family stage that produced an equal-amount match."""

    PRIMARY_ACCOUNTS = "PRIMARY_ACCOUNTS"
    TAXES_PAYABLE = "TAXES_PAYABLE"


class RefundReceiptStatus(StrEnum):
    """Whether a unique refund receipt was found in the eligible SAP lines."""

    NOT_RECEIVED = "NOT_RECEIVED"
    RECEIVED = "RECEIVED"
    AMBIGUOUS = "AMBIGUOUS"


class RefundBookingStatus(StrEnum):
    """Booking-account conclusion for the receipt match."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    CORRECT = "CORRECT"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class RefundScanPeriod:
    """A permitted monthly scan period in the March-to-December window."""

    year: int
    month: int

    def __post_init__(self) -> None:
        _require_year(self.year, "scan_period.year")
        if type(self.month) is not int:
            raise TypeError("scan_period.month must be an integer")
        if not 3 <= self.month <= 12:
            raise ValueError("income-tax refund scans are permitted only from March to December")


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundCandidate:
    """One SAP line considered by the deterministic refund matcher."""

    line_id: str
    account_family: RefundAccountFamily
    account_code: str
    account_name: str
    document_number: str
    line_item: str
    posting_date: date
    amount: Money
    is_credit: bool
    is_reversed: bool

    def __post_init__(self) -> None:
        for field_name in (
            "line_id",
            "account_code",
            "account_name",
            "document_number",
            "line_item",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.account_family, RefundAccountFamily):
            raise TypeError("account_family must be RefundAccountFamily")
        if type(self.posting_date) is not date:
            raise TypeError("posting_date must be a date")
        _require_money(self.amount, "candidate amount")
        if self.amount.quantized().amount <= 0:
            raise ValueError("candidate amount must be positive after quantization")
        if type(self.is_credit) is not bool:
            raise TypeError("is_credit must be a boolean")
        if type(self.is_reversed) is not bool:
            raise TypeError("is_reversed must be a boolean")


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundInputs:
    """Frozen inputs for one company and one monthly refund scan."""

    refund_tax_year: int
    scan_period: RefundScanPeriod
    expected_refund_amount: Money
    candidates: tuple[IncomeTaxRefundCandidate, ...]

    def __post_init__(self) -> None:
        _require_year(self.refund_tax_year, "refund_tax_year")
        if not isinstance(self.scan_period, RefundScanPeriod):
            raise TypeError("scan_period must be RefundScanPeriod")
        if self.scan_period.year != self.refund_tax_year + 1:
            raise ValueError("scan year must equal refund tax year plus one")
        _require_money(self.expected_refund_amount, "expected_refund_amount")
        if self.expected_refund_amount.quantized().amount <= 0:
            raise ValueError("expected_refund_amount must be positive after quantization")
        if type(self.candidates) is not tuple:
            raise TypeError("candidates must be an immutable tuple")
        for candidate in self.candidates:
            if not isinstance(candidate, IncomeTaxRefundCandidate):
                raise TypeError("every candidate must be IncomeTaxRefundCandidate")
            if candidate.amount.currency != self.expected_refund_amount.currency:
                raise ValueError("candidate and expected refund must use one currency")
            if candidate.amount.scale != self.expected_refund_amount.scale:
                raise ValueError("candidate and expected refund must use one amount scale")
            if (
                candidate.posting_date.year != self.scan_period.year
                or candidate.posting_date.month > self.scan_period.month
            ):
                raise ValueError(
                    "candidate posting_date must be within the scan year through scan month"
                )


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundResult:
    """Deterministic receipt, booking-account, and workflow conclusion."""

    receipt_status: RefundReceiptStatus
    booking_status: RefundBookingStatus
    normalized_expected_refund_amount: Money
    matched_candidates: tuple[IncomeTaxRefundCandidate, ...]
    match_stage: RefundMatchStage | None
    continue_scanning: bool
    requires_writeback: bool
    alert_flag: bool
    alert_code: str | None
    risk_case_required: bool


def evaluate_income_tax_refund(inputs: IncomeTaxRefundInputs) -> IncomeTaxRefundResult:
    """Match eligible SAP credit lines to the normalized expected refund amount."""

    if not isinstance(inputs, IncomeTaxRefundInputs):
        raise TypeError("evaluate_income_tax_refund requires IncomeTaxRefundInputs")

    expected = inputs.expected_refund_amount.quantized()
    eligible_matches = tuple(
        candidate
        for candidate in inputs.candidates
        if candidate.is_credit
        and not candidate.is_reversed
        and candidate.amount.quantized().amount == expected.amount
    )
    primary_matches = tuple(
        candidate
        for candidate in eligible_matches
        if candidate.account_family
        in {
            RefundAccountFamily.INCOME_TAX_EXPENSE,
            RefundAccountFamily.OTHER_INCOME,
        }
    )
    if primary_matches:
        return _resolve_matches(
            expected,
            primary_matches,
            match_stage=RefundMatchStage.PRIMARY_ACCOUNTS,
        )

    taxes_payable_matches = tuple(
        candidate
        for candidate in eligible_matches
        if candidate.account_family is RefundAccountFamily.TAXES_PAYABLE
    )
    if taxes_payable_matches:
        return _resolve_matches(
            expected,
            taxes_payable_matches,
            match_stage=RefundMatchStage.TAXES_PAYABLE,
        )

    return IncomeTaxRefundResult(
        receipt_status=RefundReceiptStatus.NOT_RECEIVED,
        booking_status=RefundBookingStatus.NOT_APPLICABLE,
        normalized_expected_refund_amount=expected,
        matched_candidates=(),
        match_stage=None,
        continue_scanning=True,
        requires_writeback=False,
        alert_flag=False,
        alert_code=None,
        risk_case_required=False,
    )


def _resolve_matches(
    expected: Money,
    matched: tuple[IncomeTaxRefundCandidate, ...],
    *,
    match_stage: RefundMatchStage,
) -> IncomeTaxRefundResult:
    if not matched:
        raise ValueError("matched candidates must be non-empty")
    if len(matched) > 1:
        return IncomeTaxRefundResult(
            receipt_status=RefundReceiptStatus.AMBIGUOUS,
            booking_status=RefundBookingStatus.AMBIGUOUS,
            normalized_expected_refund_amount=expected,
            matched_candidates=matched,
            match_stage=match_stage,
            continue_scanning=True,
            requires_writeback=False,
            alert_flag=True,
            alert_code=AMBIGUOUS_MATCH_ALERT_CODE,
            risk_case_required=True,
        )
    candidate = matched[0]
    wrong_account = candidate.account_family is not RefundAccountFamily.INCOME_TAX_EXPENSE
    return IncomeTaxRefundResult(
        receipt_status=RefundReceiptStatus.RECEIVED,
        booking_status=(
            RefundBookingStatus.WRONG_ACCOUNT if wrong_account else RefundBookingStatus.CORRECT
        ),
        normalized_expected_refund_amount=expected,
        matched_candidates=matched,
        match_stage=match_stage,
        continue_scanning=False,
        requires_writeback=True,
        alert_flag=wrong_account,
        alert_code=WRONG_ACCOUNT_ALERT_CODE if wrong_account else None,
        risk_case_required=wrong_account,
    )


def _require_year(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if not 2000 <= value <= 9999:
        raise ValueError(f"{field_name} must be between 2000 and 9999")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _require_money(value: object, field_name: str) -> Money:
    if not isinstance(value, Money):
        raise TypeError(f"{field_name} must be Money")
    if re.fullmatch(r"[A-Z]{3}", value.currency) is None:
        raise ValueError(f"{field_name} currency must use three uppercase letters")
    if value.scale > 12:
        raise ValueError(f"{field_name} scale must not exceed 12")
    return value


__all__ = [
    "AMBIGUOUS_MATCH_ALERT_CODE",
    "IncomeTaxRefundCandidate",
    "IncomeTaxRefundInputs",
    "IncomeTaxRefundResult",
    "RefundAccountFamily",
    "RefundBookingStatus",
    "RefundMatchStage",
    "RefundReceiptStatus",
    "RefundScanPeriod",
    "WRONG_ACCOUNT_ALERT_CODE",
    "evaluate_income_tax_refund",
]

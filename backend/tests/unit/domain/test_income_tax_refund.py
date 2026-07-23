from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal

import pytest

from tax_risk.domain.cases import MonitorType
from tax_risk.domain.income_tax_refund import (
    AMBIGUOUS_MATCH_ALERT_CODE,
    IncomeTaxRefundCandidate,
    IncomeTaxRefundInputs,
    RefundAccountFamily,
    RefundBookingStatus,
    RefundMatchStage,
    RefundReceiptStatus,
    RefundScanPeriod,
    WRONG_ACCOUNT_ALERT_CODE,
    evaluate_income_tax_refund,
)
from tax_risk.domain.money import Money


def _money(
    value: str = "100.00",
    *,
    currency: str = "CNY",
    scale: int = 2,
) -> Money:
    return Money.unrounded(value, currency=currency, scale=scale)


def _candidate(
    *,
    line_id: str = "line-1",
    family: RefundAccountFamily = RefundAccountFamily.INCOME_TAX_EXPENSE,
    amount: Money | None = None,
    posting_date: date = date(2026, 3, 15),
    is_credit: bool = True,
    is_reversed: bool = False,
) -> IncomeTaxRefundCandidate:
    return IncomeTaxRefundCandidate(
        line_id=line_id,
        account_family=family,
        account_code={
            RefundAccountFamily.INCOME_TAX_EXPENSE: "6801010000",
            RefundAccountFamily.OTHER_INCOME: "6112010000",
            RefundAccountFamily.TAXES_PAYABLE: "2221130000",
        }[family],
        account_name={
            RefundAccountFamily.INCOME_TAX_EXPENSE: "income tax expense",
            RefundAccountFamily.OTHER_INCOME: "other income",
            RefundAccountFamily.TAXES_PAYABLE: "taxes payable",
        }[family],
        document_number="190000001",
        line_item="001",
        posting_date=posting_date,
        amount=amount or _money(),
        is_credit=is_credit,
        is_reversed=is_reversed,
    )


def _inputs(
    *,
    expected: Money | None = None,
    candidates: tuple[IncomeTaxRefundCandidate, ...] = (),
    refund_tax_year: int = 2025,
    scan_period: RefundScanPeriod | None = None,
) -> IncomeTaxRefundInputs:
    return IncomeTaxRefundInputs(
        refund_tax_year=refund_tax_year,
        scan_period=scan_period or RefundScanPeriod(2026, 3),
        expected_refund_amount=expected or _money(),
        candidates=candidates,
    )


def test_monitor_type_exposes_income_tax_refund_account_accuracy() -> None:
    assert (
        MonitorType.INCOME_TAX_REFUND_ACCOUNT_ACCURACY.value == "INCOME_TAX_REFUND_ACCOUNT_ACCURACY"
    )


def test_no_eligible_equal_candidate_remains_not_received() -> None:
    inputs = _inputs(
        candidates=(
            _candidate(line_id="different", amount=_money("99.99")),
            _candidate(line_id="debit", is_credit=False),
            _candidate(line_id="reversed", is_reversed=True),
        )
    )

    result = evaluate_income_tax_refund(inputs)

    assert result.receipt_status is RefundReceiptStatus.NOT_RECEIVED
    assert result.booking_status is RefundBookingStatus.NOT_APPLICABLE
    assert result.matched_candidates == ()
    assert result.match_stage is None
    assert result.continue_scanning is True
    assert result.requires_writeback is False
    assert result.alert_flag is False
    assert result.alert_code is None
    assert result.risk_case_required is False


def test_unique_income_tax_expense_credit_is_received_and_correct() -> None:
    candidate = _candidate()

    result = evaluate_income_tax_refund(_inputs(candidates=(candidate,)))

    assert result.receipt_status is RefundReceiptStatus.RECEIVED
    assert result.booking_status is RefundBookingStatus.CORRECT
    assert result.matched_candidates == (candidate,)
    assert result.match_stage is RefundMatchStage.PRIMARY_ACCOUNTS
    assert result.continue_scanning is False
    assert result.requires_writeback is True
    assert result.alert_flag is False
    assert result.alert_code is None
    assert result.risk_case_required is False
    assert result.matched_candidates[0].line_id == "line-1"
    assert result.matched_candidates[0].account_code == "6801010000"
    assert result.matched_candidates[0].account_name == "income tax expense"
    assert result.matched_candidates[0].document_number == "190000001"
    assert result.matched_candidates[0].line_item == "001"
    assert result.matched_candidates[0].posting_date == date(2026, 3, 15)


def test_unique_other_income_credit_is_received_with_wrong_account_alert() -> None:
    candidate = _candidate(family=RefundAccountFamily.OTHER_INCOME)

    result = evaluate_income_tax_refund(_inputs(candidates=(candidate,)))

    assert result.receipt_status is RefundReceiptStatus.RECEIVED
    assert result.booking_status is RefundBookingStatus.WRONG_ACCOUNT
    assert result.matched_candidates == (candidate,)
    assert result.continue_scanning is False
    assert result.requires_writeback is True
    assert result.alert_flag is True
    assert result.alert_code == WRONG_ACCOUNT_ALERT_CODE
    assert result.risk_case_required is True
    assert result.match_stage is RefundMatchStage.PRIMARY_ACCOUNTS


def test_unique_taxes_payable_credit_is_received_with_wrong_account_alert() -> None:
    candidate = _candidate(family=RefundAccountFamily.TAXES_PAYABLE)

    result = evaluate_income_tax_refund(_inputs(candidates=(candidate,)))

    assert result.receipt_status is RefundReceiptStatus.RECEIVED
    assert result.booking_status is RefundBookingStatus.WRONG_ACCOUNT
    assert result.matched_candidates == (candidate,)
    assert result.match_stage is RefundMatchStage.TAXES_PAYABLE
    assert result.continue_scanning is False
    assert result.requires_writeback is True
    assert result.alert_flag is True
    assert result.alert_code == WRONG_ACCOUNT_ALERT_CODE
    assert result.risk_case_required is True


@pytest.mark.parametrize(
    "primary_family",
    [RefundAccountFamily.INCOME_TAX_EXPENSE, RefundAccountFamily.OTHER_INCOME],
)
def test_primary_account_match_takes_priority_over_taxes_payable(
    primary_family: RefundAccountFamily,
) -> None:
    primary = _candidate(line_id="primary", family=primary_family)
    fallback = _candidate(line_id="fallback", family=RefundAccountFamily.TAXES_PAYABLE)

    result = evaluate_income_tax_refund(_inputs(candidates=(fallback, primary)))

    assert result.matched_candidates == (primary,)
    assert result.match_stage is RefundMatchStage.PRIMARY_ACCOUNTS
    assert result.booking_status is (
        RefundBookingStatus.CORRECT
        if primary_family is RefundAccountFamily.INCOME_TAX_EXPENSE
        else RefundBookingStatus.WRONG_ACCOUNT
    )


def test_multiple_equal_eligible_candidates_are_ambiguous() -> None:
    candidates = (
        _candidate(line_id="line-1"),
        _candidate(line_id="line-2", family=RefundAccountFamily.OTHER_INCOME),
    )

    result = evaluate_income_tax_refund(_inputs(candidates=candidates))

    assert result.receipt_status is RefundReceiptStatus.AMBIGUOUS
    assert result.booking_status is RefundBookingStatus.AMBIGUOUS
    assert result.matched_candidates == candidates
    assert result.match_stage is RefundMatchStage.PRIMARY_ACCOUNTS
    assert result.continue_scanning is True
    assert result.requires_writeback is False
    assert result.alert_flag is True
    assert result.alert_code == AMBIGUOUS_MATCH_ALERT_CODE
    assert result.risk_case_required is True


def test_multiple_taxes_payable_matches_are_ambiguous_only_after_primary_miss() -> None:
    candidates = (
        _candidate(line_id="tax-1", family=RefundAccountFamily.TAXES_PAYABLE),
        _candidate(line_id="tax-2", family=RefundAccountFamily.TAXES_PAYABLE),
    )

    result = evaluate_income_tax_refund(_inputs(candidates=candidates))

    assert result.receipt_status is RefundReceiptStatus.AMBIGUOUS
    assert result.booking_status is RefundBookingStatus.AMBIGUOUS
    assert result.matched_candidates == candidates
    assert result.match_stage is RefundMatchStage.TAXES_PAYABLE
    assert result.continue_scanning is True
    assert result.requires_writeback is False
    assert result.alert_flag is True
    assert result.alert_code == AMBIGUOUS_MATCH_ALERT_CODE
    assert result.risk_case_required is True


def test_money_is_quantized_half_up_before_exact_matching() -> None:
    inputs = _inputs(
        expected=_money("31.4375"),
        candidates=(_candidate(amount=_money("31.44")),),
    )

    result = evaluate_income_tax_refund(inputs)

    assert result.receipt_status is RefundReceiptStatus.RECEIVED
    assert result.normalized_expected_refund_amount.amount == Decimal("31.44")
    assert inputs.expected_refund_amount.amount == Decimal("31.4375")


def test_quantization_is_not_an_amount_tolerance() -> None:
    inputs = _inputs(
        expected=_money("10.004"),
        candidates=(_candidate(amount=_money("10.005")),),
    )

    result = evaluate_income_tax_refund(inputs)

    assert result.normalized_expected_refund_amount.amount == Decimal("10.00")
    assert result.receipt_status is RefundReceiptStatus.NOT_RECEIVED


@pytest.mark.parametrize("month", [3, 12])
def test_scan_window_accepts_march_through_december(month: int) -> None:
    assert RefundScanPeriod(2026, month).month == month


@pytest.mark.parametrize("month", [0, 1, 2, 13])
def test_scan_window_rejects_months_outside_march_through_december(month: int) -> None:
    with pytest.raises(ValueError, match="March to December"):
        RefundScanPeriod(2026, month)


@pytest.mark.parametrize("year", [True, 2026.0, "2026"])
def test_scan_period_requires_a_strict_integer_year(year: object) -> None:
    with pytest.raises(TypeError, match="year must be an integer"):
        RefundScanPeriod(year, 3)  # type: ignore[arg-type]


@pytest.mark.parametrize("month", [True, 3.0, "03"])
def test_scan_period_requires_a_strict_integer_month(month: object) -> None:
    with pytest.raises(TypeError, match="month must be an integer"):
        RefundScanPeriod(2026, month)  # type: ignore[arg-type]


def test_refund_tax_year_and_scan_year_are_separate_and_consecutive() -> None:
    with pytest.raises(ValueError, match="plus one"):
        _inputs(scan_period=RefundScanPeriod(2025, 3))


def test_inputs_require_a_typed_scan_period() -> None:
    with pytest.raises(TypeError, match="scan_period must be RefundScanPeriod"):
        IncomeTaxRefundInputs(
            refund_tax_year=2025,
            scan_period=(2026, 3),  # type: ignore[arg-type]
            expected_refund_amount=_money(),
            candidates=(),
        )


@pytest.mark.parametrize("refund_tax_year", [True, 2025.0, "2025"])
def test_refund_tax_year_requires_a_strict_integer(refund_tax_year: object) -> None:
    with pytest.raises(TypeError, match="refund_tax_year must be an integer"):
        _inputs(refund_tax_year=refund_tax_year)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", ["0", "0.004", "-0.01"])
def test_expected_refund_must_be_positive_after_quantization(amount: str) -> None:
    with pytest.raises(ValueError, match="expected_refund_amount must be positive"):
        _inputs(expected=_money(amount))


def test_expected_refund_requires_money() -> None:
    with pytest.raises(TypeError, match="expected_refund_amount must be Money"):
        IncomeTaxRefundInputs(
            refund_tax_year=2025,
            scan_period=RefundScanPeriod(2026, 3),
            expected_refund_amount=Decimal("100"),  # type: ignore[arg-type]
            candidates=(),
        )


@pytest.mark.parametrize(
    ("expected", "message"),
    [
        (_money(currency="CNYY"), "three uppercase letters"),
        (_money(scale=13), "scale must not exceed 12"),
    ],
)
def test_expected_refund_requires_valid_currency_and_scale(
    expected: Money,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _inputs(expected=expected)


@pytest.mark.parametrize(
    ("candidate_amount", "message"),
    [
        (_money(currency="USD"), "one currency"),
        (_money(scale=3), "one amount scale"),
    ],
)
def test_candidates_must_match_expected_currency_and_scale(
    candidate_amount: Money,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _inputs(candidates=(_candidate(amount=candidate_amount),))


@pytest.mark.parametrize("amount", ["0", "0.004", "-1"])
def test_candidate_amount_must_be_positive_after_quantization(amount: str) -> None:
    with pytest.raises(ValueError, match="candidate amount must be positive"):
        _candidate(amount=_money(amount))


def test_candidate_amount_requires_money() -> None:
    with pytest.raises(TypeError, match="candidate amount must be Money"):
        IncomeTaxRefundCandidate(
            line_id="line-1",
            account_family=RefundAccountFamily.INCOME_TAX_EXPENSE,
            account_code="6801010000",
            account_name="income tax expense",
            document_number="190000001",
            line_item="001",
            posting_date=date(2026, 3, 15),
            amount=Decimal("100"),  # type: ignore[arg-type]
            is_credit=True,
            is_reversed=False,
        )


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        (_money(currency="CNYY"), "three uppercase letters"),
        (_money(scale=13), "scale must not exceed 12"),
    ],
)
def test_candidate_amount_requires_valid_currency_and_scale(
    amount: Money,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _candidate(amount=amount)


@pytest.mark.parametrize("field", ["is_credit", "is_reversed"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_candidate_flags_require_strict_booleans(field: str, value: object) -> None:
    values: dict[str, object] = {
        "line_id": "line-1",
        "account_family": RefundAccountFamily.INCOME_TAX_EXPENSE,
        "account_code": "6801010000",
        "account_name": "income tax expense",
        "document_number": "190000001",
        "line_item": "001",
        "posting_date": date(2026, 3, 15),
        "amount": _money(),
        "is_credit": True,
        "is_reversed": False,
    }
    values[field] = value

    with pytest.raises(TypeError, match="boolean"):
        IncomeTaxRefundCandidate(**values)  # type: ignore[arg-type]


def test_candidate_requires_controlled_account_family() -> None:
    with pytest.raises(TypeError, match="RefundAccountFamily"):
        IncomeTaxRefundCandidate(
            line_id="line-1",
            account_family="INCOME_TAX_EXPENSE",  # type: ignore[arg-type]
            account_code="6801010000",
            account_name="income tax expense",
            document_number="190000001",
            line_item="001",
            posting_date=date(2026, 3, 15),
            amount=_money(),
            is_credit=True,
            is_reversed=False,
        )


@pytest.mark.parametrize(
    "posting_date",
    [date(2025, 12, 31), date(2026, 4, 1), date(2027, 1, 1)],
)
def test_candidate_posting_date_must_be_within_scan_year_to_date(
    posting_date: date,
) -> None:
    with pytest.raises(ValueError, match="within the scan year through scan month"):
        _inputs(candidates=(_candidate(posting_date=posting_date),))


def test_candidate_posting_date_rejects_datetime() -> None:
    with pytest.raises(TypeError, match="posting_date must be a date"):
        _candidate(posting_date=datetime(2026, 3, 15))


@pytest.mark.parametrize(
    "field",
    ["line_id", "account_code", "account_name", "document_number", "line_item"],
)
def test_candidate_evidence_identifiers_must_be_nonempty(field: str) -> None:
    values: dict[str, object] = {
        "line_id": "line-1",
        "account_family": RefundAccountFamily.INCOME_TAX_EXPENSE,
        "account_code": "6801010000",
        "account_name": "income tax expense",
        "document_number": "190000001",
        "line_item": "001",
        "posting_date": date(2026, 3, 15),
        "amount": _money(),
        "is_credit": True,
        "is_reversed": False,
    }
    values[field] = "  "

    with pytest.raises(ValueError, match="non-empty"):
        IncomeTaxRefundCandidate(**values)  # type: ignore[arg-type]


def test_candidate_evidence_identifiers_require_strings() -> None:
    with pytest.raises(TypeError, match="line_id must be a string"):
        IncomeTaxRefundCandidate(
            line_id=1,  # type: ignore[arg-type]
            account_family=RefundAccountFamily.INCOME_TAX_EXPENSE,
            account_code="6801010000",
            account_name="income tax expense",
            document_number="190000001",
            line_item="001",
            posting_date=date(2026, 3, 15),
            amount=_money(),
            is_credit=True,
            is_reversed=False,
        )


def test_candidates_require_an_immutable_tuple() -> None:
    with pytest.raises(TypeError, match="immutable tuple"):
        IncomeTaxRefundInputs(
            refund_tax_year=2025,
            scan_period=RefundScanPeriod(2026, 3),
            expected_refund_amount=_money(),
            candidates=[_candidate()],  # type: ignore[arg-type]
        )


def test_candidates_require_typed_items() -> None:
    with pytest.raises(TypeError, match="every candidate"):
        _inputs(candidates=(object(),))  # type: ignore[arg-type]


def test_calculator_rejects_the_wrong_input_bundle() -> None:
    with pytest.raises(TypeError, match="IncomeTaxRefundInputs"):
        evaluate_income_tax_refund(object())  # type: ignore[arg-type]


def test_inputs_candidates_and_result_are_immutable() -> None:
    candidate = _candidate()
    inputs = _inputs(candidates=(candidate,))
    result = evaluate_income_tax_refund(inputs)

    with pytest.raises(FrozenInstanceError):
        candidate.line_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        inputs.refund_tax_year = 2024  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.continue_scanning = True  # type: ignore[misc]

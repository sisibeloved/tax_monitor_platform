from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.base import CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (
    DgcHesiInvoiceFieldMap,
    DgcHesiNoInvoiceAdapter,
    DgcHesiNoInvoiceError,
    DgcHesiNoInvoiceMetricAdapter,
    DgcHesiReimbursementFieldMap,
    HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


def test_calculates_ytd_difference_and_excludes_all_configured_codes() -> None:
    reimbursements = [_reimbursement("C-1", "2026-01-15", "F1000", "100.50")]
    invoices = [_invoice("C-1", "TYPE-F1000", "100.50", "30.25")]
    for index, code in enumerate(sorted(HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES)):
        claim_code = f"EXCLUDED-{index}"
        amount = str(index + 1)
        reimbursements.append(_reimbursement(claim_code, "2026-03-01", code.lower(), amount))
        invoices.append(_invoice(claim_code, f"TYPE-{code}", amount, amount))
    reimbursements.append(_reimbursement("FUTURE", "2026-07-01", "F1000", "900"))
    reimbursements.append(_reimbursement("PRIOR", "2025-12-31", "F1000", "800"))
    invoices.append(_invoice("PRIOR", "TYPE-F1000", "800", "800"))

    result = _adapter(reimbursements, invoices).adapt()

    assert result.reimbursement_expense_total == Decimal("100.50")
    assert result.invoice_approved_total == Decimal("30.25")
    assert result.hesi_no_invoice == Decimal("70.25")
    assert result.excluded_reimbursement_count == 19
    assert result.excluded_invoice_count == 19
    assert len(result.reimbursement_records) == 20
    assert len(result.invoice_records) == 20
    assert len(result.source_checksum) == 64


def test_floors_negative_difference_at_zero() -> None:
    result = _adapter(
        [_reimbursement("C-1", "2026-06-30", "F1000", "10")],
        [_invoice("C-1", "TYPE-F1000", "10", "12")],
    ).adapt()

    assert result.reimbursement_expense_total == Decimal("10")
    assert result.invoice_approved_total == Decimal("12")
    assert result.hesi_no_invoice == Decimal(0)


def test_successful_empty_sources_materialize_evidenced_zero_metric() -> None:
    result = _adapter([], []).adapt()
    adapter = DgcHesiNoInvoiceMetricAdapter(
        result,
        company_code="3000",
        fiscal_year=2026,
        fiscal_period=6,
        currency="CNY",
        amount_scale=2,
        extracted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    rows = tuple(adapter.iter_rows())

    assert len(rows) == 1
    assert isinstance(rows[0].value, CanonicalFinancialRow)
    assert rows[0].value.metric_code == "hesi_no_invoice"
    assert rows[0].value.amount == Decimal(0)
    assert rows[0].value.period == date(2026, 6, 30)


def test_rejects_cross_company_rows() -> None:
    row = _reimbursement("C-1", "2026-01-01", "F1000", "1")
    row["company_code"] = "3560"

    with pytest.raises(DgcHesiNoInvoiceError) as captured:
        _adapter([row], []).adapt()

    assert captured.value.error_code == "DGC_RESPONSE_SCOPE_MISMATCH"
    assert captured.value.source == "hesi_reimbursement"


@pytest.mark.parametrize(
    ("source", "field", "value"),
    (
        ("reimbursement", "fee_type_amount", 1.5),
        ("invoice", "approve_amount_dec", float("nan")),
        ("reimbursement", "flow_end_date", "2026/06/30"),
    ),
)
def test_rejects_inexact_amounts_and_ambiguous_dates(
    source: str,
    field: str,
    value: object,
) -> None:
    reimbursements = [_reimbursement("C-1", "2026-01-01", "F1000", "1")]
    invoices = [_invoice("C-1", "TYPE-F1000", "1", "1")]
    target = reimbursements[0] if source == "reimbursement" else invoices[0]
    target[field] = value

    with pytest.raises(DgcHesiNoInvoiceError) as captured:
        _adapter(reimbursements, invoices).adapt()

    assert captured.value.error_code == "INVALID_RESPONSE_VALUE"


def test_supports_explicit_source_field_maps() -> None:
    result = DgcHesiNoInvoiceAdapter(
        DgcFetchResult(
            records=(
                {
                    "corp": "3000",
                    "approved": "2026-06-01",
                    "claim": "C-1",
                    "cost_code": "F1000",
                    "cost_amount": "50",
                },
            ),
            checksum="c" * 64,
        ),
        DgcFetchResult(
            records=(
                {
                    "corp": "3000",
                    "claim": "C-1",
                    "cost_type_id": "TYPE-F1000",
                    "cost_line_amount": "50",
                    "approved_invoice": "20",
                },
            ),
            checksum="d" * 64,
        ),
        reimbursement_field_map=DgcHesiReimbursementFieldMap(
            company_code="corp",
            approval_completed_at="approved",
            expense_claim_code="claim",
            expense_type_code="cost_code",
            expense_type_amount="cost_amount",
        ),
        invoice_field_map=DgcHesiInvoiceFieldMap(
            company_code="corp",
            expense_claim_code="claim",
            expense_type_id="cost_type_id",
            expense_line_amount="cost_line_amount",
            invoice_approved_amount="approved_invoice",
        ),
        expected_company_code="3000",
        fiscal_year=2026,
        through_period=6,
    ).adapt()

    assert result.hesi_no_invoice == Decimal("30")


def test_skips_unfinished_claim_and_its_invoice() -> None:
    result = _adapter(
        [_reimbursement("C-1", None, "F1000", "50")],
        [_invoice("C-1", "TYPE-F1000", "50", "20")],
    ).adapt()

    assert result.reimbursement_records == ()
    assert result.invoice_records == ()
    assert result.hesi_no_invoice == Decimal(0)


def test_resolves_same_amount_multi_type_claim_from_stable_expense_type_id() -> None:
    result = _adapter(
        [
            _reimbursement("CALIBRATION", "2026-01-01", "F1000", "50"),
            _reimbursement("MULTI", "2026-02-01", "F1000", "100"),
            _reimbursement("MULTI", "2026-02-01", "CLF0101", "100"),
        ],
        [
            _invoice("CALIBRATION", "TYPE-F1000", "50", "30"),
            _invoice("MULTI", "TYPE-F1000", "100", "40"),
        ],
    ).adapt()

    assert result.reimbursement_expense_total == Decimal("150")
    assert result.invoice_approved_total == Decimal("70")
    assert result.hesi_no_invoice == Decimal("80")


def test_accepts_ambiguous_codes_when_all_candidates_share_exclusion_status() -> None:
    result = _adapter(
        [
            _reimbursement("MULTI", "2026-02-01", "F1000", "100"),
            _reimbursement("MULTI", "2026-02-01", "F1001", "100"),
        ],
        [_invoice("MULTI", "UNKNOWN-TYPE", "100", "40")],
    ).adapt()

    assert result.invoice_records[0].expense_type_candidates == ("F1000", "F1001")
    assert result.invoice_records[0].excluded_expense_type is False
    assert result.reimbursement_expense_total == Decimal("200")
    assert result.invoice_approved_total == Decimal("40")
    assert result.hesi_no_invoice == Decimal("160")


def test_rejects_unresolved_multi_type_invoice_mapping() -> None:
    with pytest.raises(DgcHesiNoInvoiceError) as captured:
        _adapter(
            [
                _reimbursement("MULTI", "2026-02-01", "F1000", "100"),
                _reimbursement("MULTI", "2026-02-01", "CLF0101", "100"),
            ],
            [_invoice("MULTI", "UNKNOWN-TYPE", "100", "40")],
        ).adapt()

    assert captured.value.error_code == "UNRESOLVED_INVOICE_EXPENSE_TYPE"


def test_rejects_invoice_without_matching_reimbursement_claim() -> None:
    with pytest.raises(DgcHesiNoInvoiceError) as captured:
        _adapter([], [_invoice("MISSING", "TYPE-F1000", "10", "10")]).adapt()

    assert captured.value.error_code == "UNMATCHED_INVOICE_CLAIM"


def _adapter(
    reimbursements: list[dict[str, object]],
    invoices: list[dict[str, object]],
) -> DgcHesiNoInvoiceAdapter:
    return DgcHesiNoInvoiceAdapter(
        DgcFetchResult(records=tuple(reimbursements), checksum="a" * 64),
        DgcFetchResult(records=tuple(invoices), checksum="b" * 64),
        reimbursement_field_map=DgcHesiReimbursementFieldMap(),
        invoice_field_map=DgcHesiInvoiceFieldMap(),
        expected_company_code="3000",
        fiscal_year=2026,
        through_period=6,
    )


def _reimbursement(
    claim_code: str,
    approved: str | None,
    expense_type_code: str,
    amount: object,
) -> dict[str, object]:
    return {
        "company_code": "3000",
        "flow_end_date": approved,
        "expense_code": claim_code,
        "fee_type_code": expense_type_code,
        "fee_type_amount": amount,
        "unused_source_field": "allowed",
    }


def _invoice(
    claim_code: str,
    expense_type_id: str,
    expense_line_amount: object,
    approved_amount: object,
) -> dict[str, object]:
    return {
        "company_code": "3000",
        "code": claim_code,
        "feetypeid": expense_type_id,
        "amount_standard_dec": expense_line_amount,
        "approve_amount_dec": approved_amount,
        "unused_source_field": "allowed",
    }

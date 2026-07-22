from decimal import Decimal

import pytest

from tax_risk.application.tax_adjustment_accounts.business_entertainment import (
    BusinessEntertainmentAccountCheckService,
    BusinessEntertainmentCheckRequest,
    BusinessEntertainmentEvidenceSource,
    BusinessEntertainmentLabel,
    HesiApplicationRow,
    HesiDetailRow,
    HesiInvoiceRow,
    business_entertainment_account_is_in_scope,
    classify_business_entertainment_text,
    extract_hesi_document_code,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (
    CheckStatus,
    SettlementAdjustmentRow,
)


class Source:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, ...]] = []

    def fetch_rows(self, **parameters: str) -> tuple[object, ...]:
        self.calls.append(tuple(parameters.values()))
        return self.rows


def _row(
    *,
    voucher: str,
    detail: str = "客户业务接待",
    original_doc: str = "SAP-1",
    period: str = "001",
    account: str = "6600400000",
    amount: str = "100",
    currency: str = "CNY",
    company: str = "3HD0",
    year: str = "2025",
) -> SettlementAdjustmentRow:
    return SettlementAdjustmentRow(
        company=company,
        fiscal_year=year,
        fiscal_period=period,
        voucher_no=voucher,
        detail_text=detail,
        amount_ksl=Decimal(amount),
        gl_account=account,
        account_name="业务招待费",
        group_currency=currency,
        original_system_doc_no=original_doc,
    )


def _request(through_month: int = 3) -> BusinessEntertainmentCheckRequest:
    return BusinessEntertainmentCheckRequest(
        company="3HD0",
        fiscal_year="2025",
        through_month=through_month,
    )


def _service(
    settlement_rows: tuple[SettlementAdjustmentRow, ...],
    *,
    details: tuple[HesiDetailRow, ...] = (),
    invoices: tuple[HesiInvoiceRow, ...] = (),
    applications: tuple[HesiApplicationRow, ...] = (),
) -> tuple[BusinessEntertainmentAccountCheckService, Source, Source, Source]:
    detail_source = Source(details)
    invoice_source = Source(invoices)
    application_source = Source(applications)
    return (
        BusinessEntertainmentAccountCheckService(
            settlement_source=Source(settlement_rows),  # type: ignore[arg-type]
            hesi_detail_source=detail_source,  # type: ignore[arg-type]
            hesi_invoice_source=invoice_source,  # type: ignore[arg-type]
            hesi_application_source=application_source,  # type: ignore[arg-type]
        ),
        detail_source,
        invoice_source,
        application_source,
    )


def test_account_and_hs_document_code_require_exact_rules() -> None:
    assert business_entertainment_account_is_in_scope("6600400000") is True
    assert business_entertainment_account_is_in_scope("6600400001") is False
    assert extract_hesi_document_code(" HS B26070573 ") == "B26070573"
    assert extract_hesi_document_code("SAP-B26070573") is None


def test_text_can_retain_both_abnormal_labels() -> None:
    decision = classify_business_entertainment_text("员工聚餐培训班签到")

    assert decision.status is CheckStatus.ABNORMAL
    assert decision.labels == (
        BusinessEntertainmentLabel.EMPLOYEE_WELFARE,
        BusinessEntertainmentLabel.MEETING_OR_EDUCATION,
    )
    assert decision.matched_keywords == ("员工聚餐", "签到", "培训班")


def test_settlement_keyword_hit_stops_before_hesi_queries() -> None:
    service, details, invoices, applications = _service(
        (_row(voucher="1", detail="年会员工聚餐", original_doc="HSB1"),)
    )

    result = service.run(_request())

    checked = result.details[0]
    assert checked.status is CheckStatus.ABNORMAL
    assert checked.decision_source is BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT
    assert details.calls == invoices.calls == applications.calls == []


def test_hesi_detail_keyword_hit_stops_before_invoice_and_application() -> None:
    service, details, invoices, applications = _service(
        (_row(voucher="1", detail="接待事项", original_doc="HSB1"),),
        details=(
            HesiDetailRow(
                company_code="3HD0",
                document_code="B1",
                description="内部会议餐",
            ),
        ),
    )

    checked = service.run(_request()).details[0]

    assert checked.labels == (BusinessEntertainmentLabel.EMPLOYEE_WELFARE,)
    assert checked.decision_source is BusinessEntertainmentEvidenceSource.HESI_DETAIL_DESCRIPTION
    assert checked.hesi_detail_match_count == 1
    assert details.calls == [("3HD0",)]
    assert invoices.calls == applications.calls == []


def test_full_chain_uses_all_invoice_links_and_application_descriptions() -> None:
    service, details, invoices, applications = _service(
        (_row(voucher="1", detail="接待事项", original_doc="HSB1"),),
        details=(HesiDetailRow(company_code="3HD0", document_code="B1", description="接待"),),
        invoices=(
            HesiInvoiceRow(company_code="3HD0", code="B1", reception_apply_code="A1"),
            HesiInvoiceRow(company_code="3HD0", code="B1", reception_apply_code="A2"),
        ),
        applications=(
            HesiApplicationRow(company_code="3HD0", code="A1", description="客户接待"),
            HesiApplicationRow(company_code="3HD0", code="A2", description="会议通知及议程"),
        ),
    )

    result = service.run(_request())
    checked = result.details[0]

    assert checked.labels == (BusinessEntertainmentLabel.MEETING_OR_EDUCATION,)
    assert (
        checked.decision_source is BusinessEntertainmentEvidenceSource.HESI_APPLICATION_DESCRIPTION
    )
    assert checked.reception_apply_codes == ("A1", "A2")
    assert checked.hesi_invoice_match_count == 2
    assert checked.hesi_application_match_count == 2
    assert result.hesi_detail_source_row_count == 1
    assert result.hesi_invoice_source_row_count == 2
    assert result.hesi_application_source_row_count == 2
    assert details.calls == invoices.calls == applications.calls == [("3HD0",)]


def test_completed_chain_without_keywords_is_reasonable_and_sources_are_cached() -> None:
    service, details, invoices, applications = _service(
        (
            _row(voucher="1", original_doc="HSB1"),
            _row(voucher="2", original_doc="HSB2"),
        ),
        details=(
            HesiDetailRow(company_code="3HD0", document_code="B1", description="客户接待"),
            HesiDetailRow(company_code="3HD0", document_code="B2", description="供应商接待"),
        ),
        invoices=(
            HesiInvoiceRow(company_code="3HD0", code="B1", reception_apply_code="A1"),
            HesiInvoiceRow(company_code="3HD0", code="B2", reception_apply_code="A2"),
        ),
        applications=(
            HesiApplicationRow(company_code="3HD0", code="A1", description="商务宴请"),
            HesiApplicationRow(company_code="3HD0", code="A2", description="客户拜访"),
        ),
    )

    result = service.run(_request())

    assert all(detail.status is CheckStatus.NORMAL for detail in result.details)
    assert all(
        detail.decision_source is BusinessEntertainmentEvidenceSource.RULE_CHAIN_COMPLETE
        for detail in result.details
    )
    assert details.calls == invoices.calls == applications.calls == [("3HD0",)]


def test_non_hs_unclear_row_is_reasonable_without_enrichment() -> None:
    service, details, invoices, applications = _service((_row(voucher="1"),))

    checked = service.run(_request()).details[0]

    assert checked.status is CheckStatus.NORMAL
    assert checked.labels == (BusinessEntertainmentLabel.REASONABLE,)
    assert checked.evaluated_sources == (
        BusinessEntertainmentEvidenceSource.SETTLEMENT_DETAIL_TEXT,
    )
    assert details.calls == invoices.calls == applications.calls == []


def test_cumulative_period_account_currency_and_signed_amount_summaries() -> None:
    service, _, _, _ = _service(
        (
            _row(voucher="1", period="001", amount="100"),
            _row(voucher="2", period="002", detail="培训餐", amount="-20"),
            _row(voucher="3", period="003", currency="USD", amount="5"),
            _row(voucher="4", period="004", amount="999"),
            _row(voucher="5", period="001", account="6600400001", amount="888"),
        )
    )

    result = service.run(_request())

    assert result.source_row_count == 5
    assert result.in_scope_source_row_count == 4
    assert result.eligible_detail_count == 3
    assert [(item.currency, item.amount) for item in result.currency_summaries] == [
        ("CNY", Decimal("80")),
        ("USD", Decimal("5")),
    ]
    assert len(result.monthly_summaries) == 6
    assert result.monthly_summaries[2].amount == Decimal("-20")


def test_company_scope_mismatch_is_rejected() -> None:
    service, _, _, _ = _service(
        (_row(voucher="1", original_doc="HSB1"),),
        details=(HesiDetailRow(company_code="OTHER", document_code="B1"),),
    )

    with pytest.raises(ValueError, match="company scope"):
        service.run(_request())

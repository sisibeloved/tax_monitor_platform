from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook

from tax_risk.application.business_entertainment.export import (
    BusinessEntertainmentExportRow,
    escape_excel_text,
    render_xlsx,
)


def test_formula_like_text_is_escaped_but_negative_amount_stays_numeric() -> None:
    row = BusinessEntertainmentExportRow(
        case_id=uuid4(),
        company_code="=CMD()",
        status="NEW",
        sap_link_status="PENDING_LOCATION",
        source_mode="BUSINESS_DOCUMENT_UNLINKED",
        sap_document_number=None,
        sap_line_item=None,
        semantic_label="+恶意公式",
        risk_amount=Decimal("-12.34"),
        currency="CNY",
        risk_amount_source="SAP",
        confidence_tier="HIGH",
        recommended_account_ids="@A001",
        evidence_refs="-引用",
        account_dictionary_version="v1",
        workflow_note="=1+1",
    )

    assert escape_excel_text("@SUM(A1)") == "'@SUM(A1)"
    workbook = load_workbook(BytesIO(render_xlsx((row,))), data_only=False)
    try:
        values = [cell.value for cell in workbook.active[2]]
    finally:
        workbook.close()

    assert values[1] == "'=CMD()"
    assert values[7] == "'+恶意公式"
    assert values[8] == -12.34
    assert isinstance(values[8], float)
    assert values[12] == "'@A001"
    assert values[13] == "'-引用"
    assert values[15] == "'=1+1"


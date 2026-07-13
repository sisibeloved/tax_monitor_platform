"""Versioned, formula-safe XLSX export built from shared root-case rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from uuid import UUID

from openpyxl import Workbook  # type: ignore[import-untyped]

from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentRootCase,
)


EXPORT_SCHEMA_VERSION = "business-entertainment-root-cases-v1"
EXPORT_COLUMNS = (
    "风险事项ID",
    "公司编码",
    "状态",
    "SAP关联状态",
    "来源模式",
    "SAP凭证号",
    "SAP行项目",
    "语义标签",
    "风险金额",
    "币种",
    "金额来源",
    "置信度",
    "建议科目ID",
    "证据引用",
    "科目字典版本",
    "流程提示",
)


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentExportRow:
    case_id: UUID
    company_code: str
    status: str
    sap_link_status: str
    source_mode: str
    sap_document_number: str | None
    sap_line_item: str | None
    semantic_label: str
    risk_amount: Decimal
    currency: str
    risk_amount_source: str
    confidence_tier: str
    recommended_account_ids: str
    evidence_refs: str
    account_dictionary_version: str
    workflow_note: str


def build_export_rows(
    rows: tuple[BusinessEntertainmentRootCase, ...],
) -> tuple[BusinessEntertainmentExportRow, ...]:
    return tuple(
        BusinessEntertainmentExportRow(
            case_id=row.case_id,
            company_code=row.company_code,
            status=row.status,
            sap_link_status=row.sap_link_status,
            source_mode=row.source_mode,
            sap_document_number=row.sap_document_number,
            sap_line_item=row.sap_line_item,
            semantic_label=row.semantic_label,
            risk_amount=row.risk_amount,
            currency=row.currency,
            risk_amount_source=row.risk_amount_source,
            confidence_tier=row.confidence_tier,
            recommended_account_ids=",".join(row.recommended_account_ids),
            evidence_refs=";".join(
                f"{ref.get('field_name', '')}:{ref.get('quoted_text', '')}"
                for ref in row.evidence_refs
            ),
            account_dictionary_version=row.account_dictionary_version,
            workflow_note=row.workflow_note,
        )
        for row in rows
    )


def render_xlsx(rows: tuple[BusinessEntertainmentExportRow, ...]) -> bytes:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("业务招待费风险")
    worksheet.append(EXPORT_COLUMNS)
    for row in rows:
        worksheet.append(
            (
                str(row.case_id),
                escape_excel_text(row.company_code),
                escape_excel_text(row.status),
                escape_excel_text(row.sap_link_status),
                escape_excel_text(row.source_mode),
                escape_excel_text(row.sap_document_number or ""),
                escape_excel_text(row.sap_line_item or ""),
                escape_excel_text(row.semantic_label),
                row.risk_amount,
                escape_excel_text(row.currency),
                escape_excel_text(row.risk_amount_source),
                escape_excel_text(row.confidence_tier),
                escape_excel_text(row.recommended_account_ids),
                escape_excel_text(row.evidence_refs),
                escape_excel_text(row.account_dictionary_version),
                escape_excel_text(row.workflow_note),
            )
        )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def escape_excel_text(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


__all__ = [
    "BusinessEntertainmentExportRow",
    "EXPORT_COLUMNS",
    "EXPORT_SCHEMA_VERSION",
    "build_export_rows",
    "escape_excel_text",
    "render_xlsx",
]

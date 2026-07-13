"""One root-case query shared by lists, dashboard, export, and KPI."""

from __future__ import annotations

from collections.abc import Callable, Set
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from tax_risk.persistence.repositories import UnitOfWork


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentRootCase:
    case_id: UUID
    company_id: UUID
    company_code: str
    status: str
    source_mode: str
    sap_link_status: str
    sap_document_number: str | None
    sap_line_item: str | None
    risk_amount: Decimal
    currency: str
    risk_amount_source: str
    semantic_label: str
    confidence_tier: str
    recommended_account_ids: tuple[str, ...]
    evidence_refs: tuple[dict[str, str], ...]
    account_dictionary_version: str
    workflow_note: str
    row_version: int


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentSummary:
    root_case_count: int
    total_risk_amount: Decimal
    linked_count: int
    pending_location_count: int


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentKpi:
    risk_count: int
    risk_amount: Decimal


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentCaseAudit:
    case_id: UUID
    merged_into_case_id: UUID | None
    row_version: int


class BusinessEntertainmentReportingService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def list_root_cases(
        self,
        *,
        company_scope: Set[UUID] | None,
    ) -> tuple[BusinessEntertainmentRootCase, ...]:
        with self._uow_factory() as uow:
            rows = uow.business_entertainment_scope.root_case_rows(
                company_scope=company_scope
            )
            return tuple(
                BusinessEntertainmentRootCase(
                    case_id=risk_case.id,
                    company_id=risk_case.company_id,
                    company_code=company.company_code,
                    status=risk_case.status.value,
                    source_mode=detail.source_mode,
                    sap_link_status=detail.sap_link_status,
                    sap_document_number=detection.sap_document_number,
                    sap_line_item=detection.sap_line_item,
                    risk_amount=risk_case.risk_amount or Decimal("0"),
                    currency=risk_case.currency,
                    risk_amount_source=detail.risk_amount_source,
                    semantic_label=detection.semantic_label,
                    confidence_tier=detail.confidence_tier,
                    recommended_account_ids=tuple(detection.recommended_account_ids),
                    evidence_refs=tuple(detection.evidence_refs),
                    account_dictionary_version=detail.account_dictionary_version,
                    workflow_note=detail.workflow_note,
                    row_version=risk_case.row_version,
                )
                for risk_case, detail, detection, company in rows
            )

    def summarize(
        self,
        *,
        company_scope: Set[UUID] | None,
    ) -> BusinessEntertainmentSummary:
        rows = self.list_root_cases(company_scope=company_scope)
        return BusinessEntertainmentSummary(
            root_case_count=len(rows),
            total_risk_amount=sum(
                (row.risk_amount for row in rows),
                start=Decimal("0"),
            ),
            linked_count=sum(row.sap_link_status == "LINKED" for row in rows),
            pending_location_count=sum(
                row.sap_link_status == "PENDING_LOCATION" for row in rows
            ),
        )

    def kpi(
        self,
        *,
        company_scope: Set[UUID] | None,
    ) -> BusinessEntertainmentKpi:
        summary = self.summarize(company_scope=company_scope)
        return BusinessEntertainmentKpi(
            risk_count=summary.root_case_count,
            risk_amount=summary.total_risk_amount,
        )

    def get_case_for_audit(
        self,
        case_id: UUID,
        *,
        company_scope: Set[UUID] | None,
    ) -> BusinessEntertainmentCaseAudit:
        with self._uow_factory() as uow:
            risk_case = uow.risks.get_case(case_id)
            if (
                risk_case is None
                or (
                    company_scope is not None
                    and risk_case.company_id not in company_scope
                )
            ):
                raise LookupError("risk case was not found")
            return BusinessEntertainmentCaseAudit(
                case_id=risk_case.id,
                merged_into_case_id=risk_case.merged_into_case_id,
                row_version=risk_case.row_version,
            )


__all__ = [
    "BusinessEntertainmentCaseAudit",
    "BusinessEntertainmentKpi",
    "BusinessEntertainmentReportingService",
    "BusinessEntertainmentRootCase",
    "BusinessEntertainmentSummary",
]

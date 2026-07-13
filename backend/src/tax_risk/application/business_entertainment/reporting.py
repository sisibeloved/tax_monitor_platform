"""One root-case query shared by lists, dashboard, export, and KPI."""

from __future__ import annotations

from collections.abc import Callable, Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from tax_risk.persistence.repositories import UnitOfWork


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentRootCase:
    case_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    fiscal_year: int
    period: int
    status: str
    source_mode: str
    sap_link_status: str
    sap_document_number: str | None
    sap_line_item: str | None
    risk_amount: Decimal
    currency: str
    amount_scale: int
    risk_direction: str
    priority: int
    assignee: str | None
    risk_amount_source: str
    semantic_label: str
    confidence_tier: str
    recommended_account_ids: tuple[str, ...]
    evidence_refs: tuple[dict[str, str], ...]
    account_dictionary_version: str
    workflow_note: str
    row_version: int


@dataclass(frozen=True, slots=True)
class ResolutionEvidenceLink:
    evidence_link_id: UUID
    relation_quality: str
    matched_field: str
    sap_document_number: str
    sap_line_item: str


@dataclass(frozen=True, slots=True)
class BusinessEntertainmentCaseDetailView:
    case_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    status: str
    merged_into_case_id: UUID | None
    canonical_source_record_id: UUID
    source_mode: str
    sap_link_status: str
    sap_document_number: str | None
    sap_line_item: str | None
    risk_amount: Decimal
    currency: str
    risk_amount_source: str
    semantic_label: str
    confidence_tier: str
    evidence_refs: tuple[dict[str, str], ...]
    recommended_account_ids: tuple[str, ...]
    rationale_summary: str
    missing_evidence: tuple[str, ...]
    rule_version_id: str
    model_version_id: str
    prompt_version_id: str
    case_library_version_id: str
    account_dictionary_version: str
    workflow_note: str
    row_version: int
    resolution_evidence_links: tuple[ResolutionEvidenceLink, ...]


@dataclass(frozen=True, slots=True)
class SapLinkCoverageView:
    coverage_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    period: date
    document_number: str
    line_item: str
    amount: Decimal
    currency: str
    link_status: str
    exact_evidence_link_id: UUID | None
    evaluated_via_business_document: bool
    snapshot_id: UUID


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
        fiscal_year: int | None = None,
        period: int | None = None,
        source_mode: str | None = None,
        sap_link_status: str | None = None,
        confidence_tier: str | None = None,
        case_status: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[BusinessEntertainmentRootCase, ...]:
        with self._uow_factory() as uow:
            rows = uow.business_entertainment_scope.root_case_rows(
                company_scope=company_scope,
                fiscal_year=fiscal_year,
                period=period,
                source_mode=source_mode,
                sap_link_status=sap_link_status,
                confidence_tier=confidence_tier,
                case_status=case_status,
                company_id=company_id,
            )
            return tuple(
                BusinessEntertainmentRootCase(
                    case_id=risk_case.id,
                    company_id=risk_case.company_id,
                    company_code=company.company_code,
                    company_name=company.company_name,
                    fiscal_year=detection.fiscal_year,
                    period=detection.period,
                    status=risk_case.status.value,
                    source_mode=detail.source_mode,
                    sap_link_status=detail.sap_link_status,
                    sap_document_number=detection.sap_document_number,
                    sap_line_item=detection.sap_line_item,
                    risk_amount=risk_case.risk_amount or Decimal("0"),
                    currency=risk_case.currency,
                    amount_scale=risk_case.amount_scale,
                    risk_direction=risk_case.risk_direction,
                    priority=risk_case.priority,
                    assignee=risk_case.assignee,
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

    def get_case(
        self,
        case_id: UUID,
        *,
        company_scope: Set[UUID] | None,
    ) -> BusinessEntertainmentCaseDetailView:
        with self._uow_factory() as uow:
            row = uow.business_entertainment_scope.case_row(
                case_id,
                company_scope=company_scope,
            )
            if row is None:
                raise LookupError("risk case was not found")
            risk_case, detail, detection, company = row
            resolution_rows = (
                uow.business_entertainment_scope.exact_resolution_link_rows(
                    canonical_source_record_id=detail.canonical_source_record_id,
                    snapshot_id=detection.snapshot_id,
                    company_code=detection.company_code,
                )
                if detail.source_mode == "BUSINESS_DOCUMENT_UNLINKED"
                else []
            )
            return BusinessEntertainmentCaseDetailView(
                case_id=risk_case.id,
                company_id=risk_case.company_id,
                company_code=company.company_code,
                company_name=company.company_name,
                status=risk_case.status.value,
                merged_into_case_id=risk_case.merged_into_case_id,
                canonical_source_record_id=detail.canonical_source_record_id,
                source_mode=detail.source_mode,
                sap_link_status=detail.sap_link_status,
                sap_document_number=detection.sap_document_number,
                sap_line_item=detection.sap_line_item,
                risk_amount=risk_case.risk_amount or Decimal("0"),
                currency=risk_case.currency,
                risk_amount_source=detail.risk_amount_source,
                semantic_label=detection.semantic_label,
                confidence_tier=detail.confidence_tier,
                evidence_refs=tuple(detection.evidence_refs),
                recommended_account_ids=tuple(detection.recommended_account_ids),
                rationale_summary=detection.rationale_summary,
                missing_evidence=tuple(detection.missing_evidence),
                rule_version_id=detection.rule_version_id,
                model_version_id=detection.model_version_id,
                prompt_version_id=detection.prompt_version_id,
                case_library_version_id=detection.case_library_version_id,
                account_dictionary_version=detail.account_dictionary_version,
                workflow_note=detail.workflow_note,
                row_version=risk_case.row_version,
                resolution_evidence_links=tuple(
                    ResolutionEvidenceLink(
                        evidence_link_id=link.id,
                        relation_quality=link.relation_quality,
                        matched_field=link.matched_field,
                        sap_document_number=sap.document_number,
                        sap_line_item=sap.line_item,
                    )
                    for link, sap in resolution_rows
                ),
            )

    def list_sap_link_coverage(
        self,
        *,
        company_scope: Set[UUID] | None,
        fiscal_year: int | None = None,
        period: int | None = None,
        company_id: UUID | None = None,
    ) -> tuple[SapLinkCoverageView, ...]:
        with self._uow_factory() as uow:
            rows = uow.business_entertainment_scope.sap_link_coverage_rows(
                company_scope=company_scope,
                fiscal_year=fiscal_year,
                period=period,
                company_id=company_id,
            )
            return tuple(
                SapLinkCoverageView(
                    coverage_id=coverage.id,
                    company_id=company.id,
                    company_code=company.company_code,
                    company_name=company.company_name,
                    period=coverage.period,
                    document_number=coverage.document_number,
                    line_item=coverage.line_item,
                    amount=coverage.amount,
                    currency=coverage.currency,
                    link_status=coverage.link_status,
                    exact_evidence_link_id=coverage.exact_evidence_link_id,
                    evaluated_via_business_document=(
                        coverage.evaluated_via_business_document
                    ),
                    snapshot_id=coverage.snapshot_id,
                )
                for coverage, company in rows
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
    "BusinessEntertainmentCaseDetailView",
    "BusinessEntertainmentKpi",
    "BusinessEntertainmentReportingService",
    "BusinessEntertainmentRootCase",
    "BusinessEntertainmentSummary",
    "ResolutionEvidenceLink",
    "SapLinkCoverageView",
]

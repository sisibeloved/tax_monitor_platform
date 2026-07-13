from __future__ import annotations

from collections.abc import Sequence, Set
from datetime import date
from uuid import UUID

from sqlalchemy import extract, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tax_risk.domain.business_entertainment.company_scope import ScopeVersionStatus
from tax_risk.domain.business_entertainment.evaluation import SapLinkCoverageItem
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentCaseDetail,
    BusinessEntertainmentScopeCompany,
    BusinessEntertainmentScopeVersion,
    BusinessEntertainmentSourceObservation,
    EvidenceLink,
    SapLinkCoverage,
)
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.semantic_models import (
    SapExpenseVoucherObservation,
    SapExpenseVoucherSnapshotProjection,
    SemanticDetectionRecord,
)
from tax_risk.persistence.risk_models import MonitorType, RiskCase


class SapCoverageConflictError(Exception):
    pass


class BusinessEntertainmentScopeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_version(self, version: BusinessEntertainmentScopeVersion) -> None:
        self._session.add(version)

    def add_company(self, company: BusinessEntertainmentScopeCompany) -> None:
        self._session.add(company)

    def add_source_observation(
        self,
        observation: BusinessEntertainmentSourceObservation,
    ) -> None:
        self._session.add(observation)

    def add_evidence_link(self, evidence_link: EvidenceLink) -> None:
        self._session.add(evidence_link)

    def add_case_detail(self, detail: BusinessEntertainmentCaseDetail) -> None:
        self._session.add(detail)

    def case_detail_for_case(
        self,
        risk_case_id: UUID,
        *,
        for_update: bool = False,
    ) -> BusinessEntertainmentCaseDetail | None:
        statement = select(BusinessEntertainmentCaseDetail).where(
            BusinessEntertainmentCaseDetail.risk_case_id == risk_case_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def root_case_rows(
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
    ) -> list[tuple[RiskCase, BusinessEntertainmentCaseDetail, SemanticDetectionRecord, Company]]:
        statement = (
            select(
                RiskCase,
                BusinessEntertainmentCaseDetail,
                SemanticDetectionRecord,
                Company,
            )
            .join(
                BusinessEntertainmentCaseDetail,
                BusinessEntertainmentCaseDetail.risk_case_id == RiskCase.id,
            )
            .join(
                SemanticDetectionRecord,
                SemanticDetectionRecord.id
                == BusinessEntertainmentCaseDetail.semantic_detection_id,
            )
            .join(Company, Company.id == RiskCase.company_id)
            .where(
                RiskCase.monitor_type == MonitorType.BUSINESS_ENTERTAINMENT,
                RiskCase.merged_into_case_id.is_(None),
            )
            .order_by(Company.company_code, RiskCase.id)
        )
        if company_scope is not None:
            if not company_scope:
                return []
            statement = statement.where(RiskCase.company_id.in_(company_scope))
        if fiscal_year is not None:
            statement = statement.where(SemanticDetectionRecord.fiscal_year == fiscal_year)
        if period is not None:
            statement = statement.where(SemanticDetectionRecord.period == period)
        if source_mode is not None:
            statement = statement.where(BusinessEntertainmentCaseDetail.source_mode == source_mode)
        if sap_link_status is not None:
            statement = statement.where(
                BusinessEntertainmentCaseDetail.sap_link_status == sap_link_status
            )
        if confidence_tier is not None:
            statement = statement.where(
                BusinessEntertainmentCaseDetail.confidence_tier == confidence_tier
            )
        if case_status is not None:
            statement = statement.where(RiskCase.status == case_status)
        if company_id is not None:
            statement = statement.where(RiskCase.company_id == company_id)
        return [tuple(row) for row in self._session.execute(statement).all()]

    def case_row(
        self,
        case_id: UUID,
        *,
        company_scope: Set[UUID] | None,
    ) -> tuple[
        RiskCase,
        BusinessEntertainmentCaseDetail,
        SemanticDetectionRecord,
        Company,
    ] | None:
        statement = (
            select(
                RiskCase,
                BusinessEntertainmentCaseDetail,
                SemanticDetectionRecord,
                Company,
            )
            .join(
                BusinessEntertainmentCaseDetail,
                BusinessEntertainmentCaseDetail.risk_case_id == RiskCase.id,
            )
            .join(
                SemanticDetectionRecord,
                SemanticDetectionRecord.id
                == BusinessEntertainmentCaseDetail.semantic_detection_id,
            )
            .join(Company, Company.id == RiskCase.company_id)
            .where(
                RiskCase.id == case_id,
                RiskCase.monitor_type == MonitorType.BUSINESS_ENTERTAINMENT,
            )
        )
        if company_scope is not None:
            if not company_scope:
                return None
            statement = statement.where(RiskCase.company_id.in_(company_scope))
        row = self._session.execute(statement).one_or_none()
        return None if row is None else tuple(row)

    def exact_resolution_link_rows(
        self,
        *,
        canonical_source_record_id: UUID,
        snapshot_id: UUID,
        company_code: str,
    ) -> list[tuple[EvidenceLink, SapExpenseVoucherObservation]]:
        rows = self._session.execute(
            select(EvidenceLink, SapExpenseVoucherObservation)
            .join(
                SapExpenseVoucherObservation,
                SapExpenseVoucherObservation.source_record_id
                == EvidenceLink.target_record_id,
            )
            .where(
                EvidenceLink.source_record_id == canonical_source_record_id,
                EvidenceLink.snapshot_id == snapshot_id,
                EvidenceLink.company_code == company_code,
                EvidenceLink.relation_quality == "EXACT",
            )
            .order_by(
                SapExpenseVoucherObservation.document_number,
                SapExpenseVoucherObservation.line_item,
                EvidenceLink.id,
            )
        ).all()
        return [tuple(row) for row in rows]

    def sap_link_coverage_rows(
        self,
        *,
        company_scope: Set[UUID] | None,
        fiscal_year: int | None = None,
        period: int | None = None,
        company_id: UUID | None = None,
    ) -> list[tuple[SapLinkCoverage, Company]]:
        statement = (
            select(SapLinkCoverage, Company)
            .join(Company, Company.company_code == SapLinkCoverage.company_code)
            .order_by(
                Company.company_code,
                SapLinkCoverage.period,
                SapLinkCoverage.document_number,
                SapLinkCoverage.line_item,
            )
        )
        if company_scope is not None:
            if not company_scope:
                return []
            statement = statement.where(Company.id.in_(company_scope))
        if fiscal_year is not None:
            statement = statement.where(
                extract("year", SapLinkCoverage.period) == fiscal_year
            )
        if period is not None:
            statement = statement.where(extract("month", SapLinkCoverage.period) == period)
        if company_id is not None:
            statement = statement.where(Company.id == company_id)
        return [tuple(row) for row in self._session.execute(statement).all()]

    def evidence_links_for_snapshot(self, snapshot_id: UUID) -> list[EvidenceLink]:
        return list(
            self._session.scalars(
                select(EvidenceLink)
                .where(EvidenceLink.snapshot_id == snapshot_id)
                .order_by(
                    EvidenceLink.company_code,
                    EvidenceLink.source_record_id,
                    EvidenceLink.target_record_id,
                    EvidenceLink.relation_kind,
                )
            )
        )

    def get_evidence_link(
        self,
        evidence_link_id: UUID,
        *,
        for_update: bool = False,
    ) -> EvidenceLink | None:
        statement = select(EvidenceLink).where(EvidenceLink.id == evidence_link_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def persist_sap_link_coverages(
        self,
        items: Sequence[SapLinkCoverageItem],
    ) -> list[SapLinkCoverage]:
        ordered = tuple(
            sorted(items, key=lambda item: (str(item.snapshot_id), str(item.sap_observation_id)))
        )
        keys = [(item.snapshot_id, item.sap_observation_id) for item in ordered]
        if len(keys) != len(set(keys)):
            raise SapCoverageConflictError("duplicate SAP coverage key in one request")
        if not ordered:
            return []

        observations = {
            row.id: row
            for row in self._session.scalars(
                select(SapExpenseVoucherObservation).where(
                    SapExpenseVoucherObservation.id.in_(
                        item.sap_observation_id for item in ordered
                    )
                )
            )
        }
        projections = set(
            self._session.execute(
                select(
                    SapExpenseVoucherSnapshotProjection.snapshot_id,
                    SapExpenseVoucherSnapshotProjection.observation_id,
                    SapExpenseVoucherSnapshotProjection.period,
                ).where(
                    tuple_(
                        SapExpenseVoucherSnapshotProjection.snapshot_id,
                        SapExpenseVoucherSnapshotProjection.observation_id,
                    ).in_(keys)
                )
            ).all()
        )
        link_ids = {
            item.exact_evidence_link_id
            for item in ordered
            if item.exact_evidence_link_id is not None
        }
        links = {
            row.id: row
            for row in self._session.scalars(
                select(EvidenceLink).where(EvidenceLink.id.in_(link_ids))
            )
        }
        for item in ordered:
            observation = observations.get(item.sap_observation_id)
            if observation is None:
                raise SapCoverageConflictError("SAP coverage observation is missing")
            projection_identity = (
                item.snapshot_id,
                item.sap_observation_id,
                item.period_end,
            )
            if projection_identity not in projections:
                raise SapCoverageConflictError("SAP observation is not bound to this snapshot")
            source_identity = (
                observation.company_code,
                observation.document_number,
                observation.line_item,
                observation.amount,
                observation.currency,
            )
            item_identity = (
                item.company_code,
                item.document_number,
                item.line_item,
                item.amount,
                item.currency,
            )
            if source_identity != item_identity:
                raise SapCoverageConflictError("SAP coverage fields differ from the observation")
            if item.exact_evidence_link_id is not None:
                link = links.get(item.exact_evidence_link_id)
                if (
                    link is None
                    or link.snapshot_id != item.snapshot_id
                    or link.target_record_id != observation.source_record_id
                    or link.relation_quality != "EXACT"
                ):
                    raise SapCoverageConflictError("exact evidence does not match SAP coverage")

        self._session.execute(
            insert(SapLinkCoverage)
            .values(
                [
                    {
                        "company_code": item.company_code,
                        "period": item.period_end,
                        "sap_observation_id": item.sap_observation_id,
                        "document_number": item.document_number,
                        "line_item": item.line_item,
                        "amount": item.amount,
                        "currency": item.currency,
                        "link_status": item.link_status.value,
                        "exact_evidence_link_id": item.exact_evidence_link_id,
                        "evaluated_via_business_document": (
                            item.evaluated_via_business_document
                        ),
                        "snapshot_id": item.snapshot_id,
                    }
                    for item in ordered
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["snapshot_id", "sap_observation_id"]
            )
        )
        persisted = list(
            self._session.scalars(
                select(SapLinkCoverage)
                .where(
                    tuple_(
                        SapLinkCoverage.snapshot_id,
                        SapLinkCoverage.sap_observation_id,
                    ).in_(keys)
                )
                .order_by(SapLinkCoverage.snapshot_id, SapLinkCoverage.sap_observation_id)
            )
        )
        expected_by_key = {
            (item.snapshot_id, item.sap_observation_id): item for item in ordered
        }
        for row in persisted:
            expected = expected_by_key[(row.snapshot_id, row.sap_observation_id)]
            actual = (
                row.company_code,
                row.period,
                row.document_number,
                row.line_item,
                row.amount,
                row.currency,
                row.link_status,
                row.exact_evidence_link_id,
                row.evaluated_via_business_document,
            )
            desired = (
                expected.company_code,
                expected.period_end,
                expected.document_number,
                expected.line_item,
                expected.amount,
                expected.currency,
                expected.link_status.value,
                expected.exact_evidence_link_id,
                expected.evaluated_via_business_document,
            )
            if actual != desired:
                raise SapCoverageConflictError("idempotent SAP coverage replay changed values")
        return persisted

    def source_observations_for_batch(
        self,
        batch_id: UUID,
    ) -> list[BusinessEntertainmentSourceObservation]:
        return list(
            self._session.scalars(
                select(BusinessEntertainmentSourceObservation)
                .where(BusinessEntertainmentSourceObservation.ingest_batch_id == batch_id)
                .order_by(BusinessEntertainmentSourceObservation.source_record_key)
            )
        )

    def get_version(
        self,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> BusinessEntertainmentScopeVersion | None:
        statement = select(BusinessEntertainmentScopeVersion).where(
            BusinessEntertainmentScopeVersion.id == version_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def version_for_batch(
        self,
        batch_id: UUID,
    ) -> BusinessEntertainmentScopeVersion | None:
        return self._session.scalar(
            select(BusinessEntertainmentScopeVersion).where(
                BusinessEntertainmentScopeVersion.batch_id == batch_id
            )
        )

    def companies_for_version(self, version_id: UUID) -> list[Company]:
        return list(
            self._session.scalars(
                select(Company)
                .join(
                    BusinessEntertainmentScopeCompany,
                    BusinessEntertainmentScopeCompany.company_id == Company.id,
                )
                .where(BusinessEntertainmentScopeCompany.version_id == version_id)
                .order_by(Company.company_code)
            )
        )

    def published_for_date(
        self,
        effective_on: date,
        *,
        for_update: bool = False,
    ) -> list[BusinessEntertainmentScopeVersion]:
        statement = (
            select(BusinessEntertainmentScopeVersion)
            .where(
                BusinessEntertainmentScopeVersion.status == ScopeVersionStatus.PUBLISHED,
                BusinessEntertainmentScopeVersion.effective_from <= effective_on,
                BusinessEntertainmentScopeVersion.effective_to >= effective_on,
            )
            .order_by(BusinessEntertainmentScopeVersion.effective_from)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def overlapping_published(
        self,
        candidate: BusinessEntertainmentScopeVersion,
    ) -> list[BusinessEntertainmentScopeVersion]:
        return list(
            self._session.scalars(
                select(BusinessEntertainmentScopeVersion)
                .where(
                    BusinessEntertainmentScopeVersion.id != candidate.id,
                    BusinessEntertainmentScopeVersion.status == ScopeVersionStatus.PUBLISHED,
                    BusinessEntertainmentScopeVersion.effective_from <= candidate.effective_to,
                    BusinessEntertainmentScopeVersion.effective_to >= candidate.effective_from,
                )
                .order_by(BusinessEntertainmentScopeVersion.effective_from)
                .with_for_update()
            )
        )

    def lock_publication(self) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_namespace)"),
            {"lock_namespace": 2026071301},
        )

    def lock_import(self, source_batch_key: str) -> None:
        self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                ":lock_namespace, hashtext(:source_batch_key))"
            ),
            {
                "lock_namespace": 2026071302,
                "source_batch_key": source_batch_key,
            },
        )


__all__ = ["BusinessEntertainmentScopeRepository", "SapCoverageConflictError"]

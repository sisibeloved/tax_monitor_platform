"""Business-entertainment case creation on top of the phase-1 workflow."""

from __future__ import annotations

from uuid import UUID

from tax_risk.domain.cases import semantic_case_fingerprint
from tax_risk.domain.semantic.contracts import SemanticDetection
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentCaseDetail,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    MonitorType,
    RiskCase,
    RiskCaseStatus,
)
from tax_risk.persistence.semantic_models import SemanticDetectionRecord


def create_or_update_business_entertainment_case(
    *,
    detection: SemanticDetection,
    persisted_detection: SemanticDetectionRecord,
    company_id: UUID,
    uow: UnitOfWork,
) -> tuple[RiskCase, bool]:
    fingerprint = semantic_case_fingerprint(
        detection.company_code,
        detection.fiscal_year,
        MonitorType.BUSINESS_ENTERTAINMENT.value,
        detection.source_mode,
        detection.canonical_source_record_id,
        detection.sap_observation_id,
    )
    risk_case = uow.risks.get_case_by_fingerprint(fingerprint)
    created = risk_case is None
    if risk_case is None:
        risk_case = RiskCase(
            fingerprint=fingerprint,
            company_id=company_id,
            latest_detection_id=None,
            monitor_type=MonitorType.BUSINESS_ENTERTAINMENT,
            status=RiskCaseStatus.NEW,
            risk_amount=abs(detection.amount),
            risk_rate=None,
            currency=detection.currency,
            amount_scale=2,
            risk_direction="POTENTIAL_MISCLASSIFICATION",
            priority={"HIGH": 1, "MEDIUM": 2, "LOW": 3}[detection.confidence_tier.value],
            assignee=None,
            merged_into_case_id=None,
            lineage={
                "semantic_detection_id": str(persisted_detection.id),
                "workflow": (
                    "已关联SAP凭证"
                    if detection.source_mode == "SAP_LINKED"
                    else "待定位SAP凭证"
                ),
            },
            row_version=1,
        )
        uow.risks.add_case(risk_case)
        uow.session.flush()
        detail = BusinessEntertainmentCaseDetail(
            risk_case_id=risk_case.id,
            semantic_detection_id=persisted_detection.id,
            candidate_key=detection.candidate_key,
            canonical_source_record_id=detection.canonical_source_record_id,
            source_mode=detection.source_mode,
            sap_link_status=(
                "LINKED" if detection.source_mode == "SAP_LINKED" else "PENDING_LOCATION"
            ),
            sap_observation_id=detection.sap_observation_id,
            risk_amount_source=(
                "SAP" if detection.source_mode == "SAP_LINKED" else "BUSINESS_DOCUMENT"
            ),
            confidence_tier=detection.confidence_tier.value,
            account_dictionary_version=detection.versions.account_dictionary_version,
            exact_evidence_link_id=detection.exact_evidence_link_id,
            workflow_note=(
                "已关联SAP凭证"
                if detection.source_mode == "SAP_LINKED"
                else "待定位SAP凭证"
            ),
        )
        uow.business_entertainment_scope.add_case_detail(detail)
    else:
        existing_detail = uow.business_entertainment_scope.case_detail_for_case(
            risk_case.id,
            for_update=True,
        )
        if existing_detail is None:
            raise RuntimeError("business-entertainment case has no semantic detail")
        existing_detail.semantic_detection_id = persisted_detection.id
        existing_detail.confidence_tier = detection.confidence_tier.value
        existing_detail.account_dictionary_version = (
            detection.versions.account_dictionary_version
        )
        risk_case.risk_amount = abs(detection.amount)
        risk_case.priority = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}[
            detection.confidence_tier.value
        ]
        risk_case.lineage = {
            **risk_case.lineage,
            "semantic_detection_id": str(persisted_detection.id),
        }
        risk_case.row_version += 1
    uow.session.flush()
    return risk_case, created


__all__ = ["create_or_update_business_entertainment_case"]

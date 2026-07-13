"""Resolve a pending business-document case to SAP using persisted exact evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from tax_risk.application.cases import create_or_update_business_entertainment_case
from tax_risk.application.semantic.detection_router import semantic_detection_model
from tax_risk.domain.semantic.contracts import SemanticDetection
from tax_risk.persistence.ingest_models import SourceRecord
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import AuditEvent, MonitorType
from tax_risk.persistence.semantic_models import (
    SapExpenseVoucherObservation,
    SemanticDetectionRecord,
)


@dataclass(frozen=True, slots=True)
class CaseMergeResult:
    source_case_id: UUID
    root_case_id: UUID
    evidence_link_id: UUID
    merged: bool


class CaseMergeError(Exception):
    error_code = "CASE_MERGE_ERROR"


class CaseMergeNotFoundError(CaseMergeError):
    error_code = "CASE_MERGE_NOT_FOUND"


class CaseMergeConflictError(CaseMergeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class CaseMergeService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._inject = failure_injector or (lambda _stage: None)

    def resolve_to_sap(
        self,
        *,
        business_case_id: UUID,
        evidence_link_id: UUID,
        expected_row_version: int,
        actor: str,
    ) -> CaseMergeResult:
        with self._uow_factory() as uow:
            source_case = uow.risks.get_case(business_case_id, for_update=True)
            if source_case is None:
                raise CaseMergeNotFoundError("business case was not found")
            if source_case.monitor_type is not MonitorType.BUSINESS_ENTERTAINMENT:
                raise CaseMergeConflictError(
                    "CASE_MONITOR_TYPE_MISMATCH",
                    "case is not a business-entertainment risk",
                )
            if source_case.row_version != expected_row_version:
                raise CaseMergeConflictError(
                    "CASE_ROW_VERSION_CONFLICT",
                    "case row version changed",
                )
            source_detail = uow.business_entertainment_scope.case_detail_for_case(
                source_case.id,
                for_update=True,
            )
            if source_detail is None:
                raise CaseMergeConflictError(
                    "CASE_DETAIL_MISSING", "business case detail was not found"
                )
            relation = uow.business_entertainment_scope.get_evidence_link(
                evidence_link_id,
                for_update=True,
            )
            if relation is None:
                raise CaseMergeNotFoundError("persisted evidence link was not found")
            if relation.relation_quality != "EXACT":
                raise CaseMergeConflictError(
                    "EVIDENCE_NOT_EXACT", "only persisted EXACT evidence can resolve a case"
                )
            if source_case.merged_into_case_id is not None:
                root_detail = uow.business_entertainment_scope.case_detail_for_case(
                    source_case.merged_into_case_id
                )
                if root_detail is None or root_detail.exact_evidence_link_id != relation.id:
                    raise CaseMergeConflictError(
                        "CASE_ALREADY_MERGED",
                        "case was already merged using different evidence",
                    )
                return CaseMergeResult(
                    source_case_id=source_case.id,
                    root_case_id=source_case.merged_into_case_id,
                    evidence_link_id=relation.id,
                    merged=True,
                )
            if source_detail.source_mode != "BUSINESS_DOCUMENT_UNLINKED":
                raise CaseMergeConflictError(
                    "CASE_ALREADY_SAP_LINKED", "only pending-location cases can be resolved"
                )
            source_detection = uow.semantic.get_semantic_detection(
                source_detail.semantic_detection_id
            )
            if source_detection is None:
                raise CaseMergeConflictError(
                    "SEMANTIC_DETECTION_MISSING", "source semantic detection is missing"
                )
            source_record = uow.session.get(SourceRecord, relation.source_record_id)
            target_record = uow.session.get(SourceRecord, relation.target_record_id)
            sap = uow.semantic.sap_observation_by_source_record(relation.target_record_id)
            if (
                relation.source_record_id != source_detail.canonical_source_record_id
                or relation.snapshot_id != source_detection.snapshot_id
                or relation.company_code != source_detection.company_code
                or source_record is None
                or target_record is None
                or source_record.company_id != source_case.company_id
                or target_record.company_id != source_case.company_id
                or sap is None
                or sap.company_code != source_detection.company_code
            ):
                raise CaseMergeConflictError(
                    "EVIDENCE_LINEAGE_MISMATCH",
                    "EXACT evidence company, source, target, or snapshot lineage is invalid",
                )

            derived = _derive_linked_detection(source_detection, relation.id, sap.id, sap)
            persisted_root_detection = uow.semantic.get_semantic_detection_by_key(
                derived.detection_key
            )
            if persisted_root_detection is None:
                persisted_root_detection = semantic_detection_model(derived)
                uow.semantic.add_semantic_detection(persisted_root_detection)
                uow.session.flush()
            root_case, _ = create_or_update_business_entertainment_case(
                detection=derived,
                persisted_detection=persisted_root_detection,
                company_id=source_case.company_id,
                uow=uow,
            )
            if root_case.id == source_case.id:
                raise CaseMergeConflictError(
                    "CASE_SELF_MERGE", "source case cannot merge into itself"
                )
            source_case.merged_into_case_id = root_case.id
            source_case.row_version += 1
            uow.risks.add_audit_event(
                AuditEvent(
                    entity_type="RISK_CASE",
                    entity_id=source_case.id,
                    action="RESOLVE_TO_SAP",
                    actor=actor,
                    correlation_id=None,
                    payload={
                        "evidence_link_id": str(relation.id),
                        "root_case_id": str(root_case.id),
                    },
                )
            )
            self._inject("before_commit")
            result = CaseMergeResult(
                source_case_id=source_case.id,
                root_case_id=root_case.id,
                evidence_link_id=relation.id,
                merged=True,
            )
            uow.commit()
            return result


def _derive_linked_detection(
    source: SemanticDetectionRecord,
    evidence_link_id: UUID,
    sap_observation_id: UUID,
    sap: SapExpenseVoucherObservation,
) -> SemanticDetection:
    payload = {
        "detection_key": sha256(
            f"{source.detection_key}\0RESOLVED\0{evidence_link_id}".encode()
        ).hexdigest(),
        "candidate_key": source.candidate_key,
        "company_code": source.company_code,
        "fiscal_year": source.fiscal_year,
        "period": source.period,
        "source_mode": "SAP_LINKED",
        "canonical_source_record_id": source.canonical_source_record_id,
        "sap_observation_id": sap_observation_id,
        "sap_document_number": sap.document_number,
        "sap_line_item": sap.line_item,
        "amount": sap.amount,
        "currency": sap.currency,
        "snapshot_id": source.snapshot_id,
        "exact_evidence_link_id": evidence_link_id,
        "versions": {
            "rule_version_id": source.rule_version_id,
            "model_version_id": source.model_version_id,
            "prompt_version_id": source.prompt_version_id,
            "case_library_version_id": source.case_library_version_id,
            "account_dictionary_version": source.account_dictionary_version,
        },
        "semantic_label": source.semantic_label,
        "confidence_tier": source.confidence_tier,
        "evidence_refs": source.evidence_refs,
        "recommended_account_ids": source.recommended_account_ids,
        "rationale_summary": source.rationale_summary,
        "missing_evidence": source.missing_evidence,
        "detected_at": datetime.now(timezone.utc),
    }
    return SemanticDetection.model_validate(payload)


__all__ = [
    "CaseMergeConflictError",
    "CaseMergeNotFoundError",
    "CaseMergeResult",
    "CaseMergeService",
]

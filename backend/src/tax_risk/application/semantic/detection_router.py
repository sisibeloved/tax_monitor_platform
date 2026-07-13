"""Deterministic routing policy and transactional semantic routing service."""

from __future__ import annotations

from enum import StrEnum
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from tax_risk.application.cases import create_or_update_business_entertainment_case
from tax_risk.domain.semantic.contracts import SemanticDetection
from tax_risk.persistence.business_entertainment_models import EvidenceLink
from tax_risk.persistence.ingest_models import CompanyLifecycle, SourceRecord
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import (
    SapExpenseVoucherObservation,
    SemanticDetectionRecord,
    SemanticEvidenceTask,
)
from tax_risk.persistence.snapshot_models import AccountingSnapshot


class RoutingOutcome(StrEnum):
    DETECTION_ONLY = "DETECTION_ONLY"
    EVIDENCE_TASK = "EVIDENCE_TASK"
    RISK_CASE = "RISK_CASE"


@dataclass(frozen=True, slots=True)
class RoutingResult:
    detection_id: UUID
    outcome: RoutingOutcome
    evidence_task_id: UUID | None
    risk_case_id: UUID | None
    detection_created: bool
    case_created: bool


def decide_detection_route(
    detection: SemanticDetection,
    suspicious_labels: frozenset[str],
) -> RoutingOutcome:
    if detection.semantic_label.value == "CURRENT_ACCOUNT_REASONABLE":
        return RoutingOutcome.DETECTION_ONLY
    if detection.semantic_label.value == "INSUFFICIENT_EVIDENCE":
        return RoutingOutcome.EVIDENCE_TASK
    if detection.semantic_label.value in suspicious_labels:
        return RoutingOutcome.RISK_CASE
    return RoutingOutcome.EVIDENCE_TASK


class SemanticCaseRouter:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def route(
        self,
        detection: SemanticDetection,
        *,
        suspicious_labels: frozenset[str],
    ) -> RoutingResult:
        outcome = decide_detection_route(detection, suspicious_labels)
        with self._uow_factory() as uow:
            company = uow.ingest.get_company_by_code(detection.company_code, for_update=True)
            if company is None or company.lifecycle != CompanyLifecycle.ACTIVE:
                raise ValueError("semantic detection company is not active")
            _validate_authoritative_lineage(uow, detection, company.id)
            persisted = uow.semantic.get_semantic_detection_by_key(detection.detection_key)
            detection_created = persisted is None
            if persisted is None:
                persisted = _detection_model(detection)
                uow.semantic.add_semantic_detection(persisted)
                uow.session.flush()
            else:
                _assert_detection_replay(persisted, detection)

            evidence_task_id: UUID | None = None
            risk_case_id: UUID | None = None
            case_created = False
            if outcome is RoutingOutcome.EVIDENCE_TASK:
                task = uow.semantic.get_evidence_task_for_detection(persisted.id)
                if task is None:
                    task = SemanticEvidenceTask(
                        detection_id=persisted.id,
                        company_code=detection.company_code,
                        status="OPEN",
                        missing_evidence=detection.missing_evidence,
                        reason="模型证据不足，需补充材料后重新判断。",
                    )
                    uow.semantic.add_semantic_evidence_task(task)
                    uow.session.flush()
                evidence_task_id = task.id
            elif outcome is RoutingOutcome.RISK_CASE:
                risk_case, case_created = create_or_update_business_entertainment_case(
                    detection=detection,
                    persisted_detection=persisted,
                    company_id=company.id,
                    uow=uow,
                )
                risk_case_id = risk_case.id

            result = RoutingResult(
                detection_id=persisted.id,
                outcome=outcome,
                evidence_task_id=evidence_task_id,
                risk_case_id=risk_case_id,
                detection_created=detection_created,
                case_created=case_created,
            )
            uow.commit()
            return result


def _validate_authoritative_lineage(
    uow: UnitOfWork,
    detection: SemanticDetection,
    company_id: UUID,
) -> None:
    source = uow.session.get(SourceRecord, detection.canonical_source_record_id)
    snapshot = uow.session.get(AccountingSnapshot, detection.snapshot_id)
    if source is None or source.company_id != company_id:
        raise ValueError("canonical source does not belong to the detection company")
    if snapshot is None or snapshot.company_id != company_id:
        raise ValueError("snapshot does not belong to the detection company")
    if detection.source_mode == "SAP_LINKED":
        sap = uow.session.get(SapExpenseVoucherObservation, detection.sap_observation_id)
        if sap is None or sap.company_code != detection.company_code:
            raise ValueError("SAP observation does not belong to the detection company")
        if detection.exact_evidence_link_id is None:
            raise ValueError("SAP-linked detection requires persisted exact evidence")
        relation = uow.session.get(EvidenceLink, detection.exact_evidence_link_id)
        if (
            relation is None
            or relation.relation_quality != "EXACT"
            or relation.company_code != detection.company_code
            or relation.snapshot_id != detection.snapshot_id
            or relation.source_record_id != detection.canonical_source_record_id
            or relation.target_record_id != sap.source_record_id
        ):
            raise ValueError("persisted exact evidence does not match the detection")


def _detection_model(detection: SemanticDetection) -> SemanticDetectionRecord:
    return SemanticDetectionRecord(
        detection_key=detection.detection_key,
        candidate_key=detection.candidate_key,
        company_code=detection.company_code,
        fiscal_year=detection.fiscal_year,
        period=detection.period,
        source_mode=detection.source_mode,
        canonical_source_record_id=detection.canonical_source_record_id,
        sap_observation_id=detection.sap_observation_id,
        sap_document_number=detection.sap_document_number,
        sap_line_item=detection.sap_line_item,
        amount=detection.amount,
        currency=detection.currency,
        snapshot_id=detection.snapshot_id,
        exact_evidence_link_id=detection.exact_evidence_link_id,
        rule_version_id=detection.versions.rule_version_id,
        model_version_id=detection.versions.model_version_id,
        prompt_version_id=detection.versions.prompt_version_id,
        case_library_version_id=detection.versions.case_library_version_id,
        account_dictionary_version=detection.versions.account_dictionary_version,
        semantic_label=detection.semantic_label.value,
        confidence_tier=detection.confidence_tier.value,
        evidence_refs=[ref.model_dump(mode="json") for ref in detection.evidence_refs],
        recommended_account_ids=detection.recommended_account_ids,
        rationale_summary=detection.rationale_summary,
        missing_evidence=detection.missing_evidence,
        detected_at=detection.detected_at,
    )


def _assert_detection_replay(
    persisted: SemanticDetectionRecord,
    detection: SemanticDetection,
) -> None:
    identity = (
        persisted.candidate_key,
        persisted.company_code,
        persisted.snapshot_id,
        persisted.model_version_id,
        persisted.semantic_label,
        persisted.amount,
    )
    expected = (
        detection.candidate_key,
        detection.company_code,
        detection.snapshot_id,
        detection.versions.model_version_id,
        detection.semantic_label.value,
        detection.amount,
    )
    if identity != expected:
        raise ValueError("semantic detection idempotency replay changed authority fields")


__all__ = [
    "RoutingOutcome",
    "RoutingResult",
    "SemanticCaseRouter",
    "decide_detection_route",
]

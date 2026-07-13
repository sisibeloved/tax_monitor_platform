"""Business-document evidence packs and deterministic post-model review."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tax_risk.domain.business_entertainment.evaluation import (
    BusinessEntertainmentEvaluationItem,
)
from tax_risk.domain.semantic.contracts import (
    EvidenceRef,
    SemanticDetection,
    SemanticModelJudgment,
    SemanticVersionSet,
)


class BusinessEvidenceReviewError(ValueError):
    pass


class BusinessEvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=64, max_length=64)
    field_name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4000)
    source_record_id: UUID
    snapshot_id: UUID


class BusinessEntertainmentEvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str
    snapshot_id: UUID
    canonical_source_record_id: UUID
    fields: tuple[BusinessEvidenceField, ...]


def build_business_evidence_pack(
    *,
    candidate_key: str,
    snapshot_id: UUID,
    canonical_source_record_id: UUID,
    fields: Iterable[tuple[str, str, UUID]],
) -> BusinessEntertainmentEvidencePack:
    evidence_fields: list[BusinessEvidenceField] = []
    for field_name, value, source_record_id in fields:
        evidence_id = sha256(
            "\0".join(
                (
                    candidate_key,
                    str(snapshot_id),
                    str(source_record_id),
                    field_name,
                    value,
                )
            ).encode()
        ).hexdigest()
        evidence_fields.append(
            BusinessEvidenceField(
                evidence_id=evidence_id,
                field_name=field_name,
                value=value,
                source_record_id=source_record_id,
                snapshot_id=snapshot_id,
            )
        )
    return BusinessEntertainmentEvidencePack(
        candidate_key=candidate_key,
        snapshot_id=snapshot_id,
        canonical_source_record_id=canonical_source_record_id,
        fields=tuple(
            sorted(
                evidence_fields,
                key=lambda field: (field.field_name, str(field.source_record_id), field.evidence_id),
            )
        ),
    )


def review_and_assemble_detection(
    *,
    evaluation_item: BusinessEntertainmentEvaluationItem,
    judgment: SemanticModelJudgment,
    evidence_pack: BusinessEntertainmentEvidencePack,
    versions: SemanticVersionSet,
    account_validator: Callable[[str, str], bool],
) -> SemanticDetection:
    if (
        evidence_pack.candidate_key != evaluation_item.candidate_key
        or evidence_pack.snapshot_id != evaluation_item.snapshot_id
        or evidence_pack.canonical_source_record_id
        != evaluation_item.canonical_source_record_id
    ):
        raise BusinessEvidenceReviewError("evidence pack identity does not match evaluation")

    fields = {field.evidence_id: field for field in evidence_pack.fields}
    refs: list[EvidenceRef] = []
    for citation in judgment.evidence_citations:
        field = fields.get(citation.evidence_id)
        if field is None:
            raise BusinessEvidenceReviewError("citation references evidence outside this item")
        if citation.field_name != field.field_name:
            raise BusinessEvidenceReviewError("citation field does not match evidence")
        if citation.quoted_text not in field.value:
            raise BusinessEvidenceReviewError("citation quote was altered")
        refs.append(
            EvidenceRef(
                evidence_id=citation.evidence_id,
                field_name=citation.field_name,
                quoted_text=citation.quoted_text,
                source_record_id=field.source_record_id,
                snapshot_id=field.snapshot_id,
            )
        )
    for account_id in judgment.recommended_account_ids:
        if not account_validator(account_id, judgment.semantic_label.value):
            raise BusinessEvidenceReviewError(
                "recommended account is not compatible with the published dictionary"
            )
    _require_uncertainty_wording(judgment.rationale_summary)

    detection_key = sha256(
        "\0".join(
            (
                evaluation_item.candidate_key,
                versions.rule_version_id,
                versions.model_version_id,
                versions.prompt_version_id,
                versions.case_library_version_id,
                versions.account_dictionary_version,
            )
        ).encode()
    ).hexdigest()
    return SemanticDetection(
        detection_key=detection_key,
        candidate_key=evaluation_item.candidate_key,
        company_code=evaluation_item.company_code,
        fiscal_year=evaluation_item.fiscal_year,
        period=evaluation_item.period,
        source_mode=evaluation_item.source_mode.value,
        canonical_source_record_id=evaluation_item.canonical_source_record_id,
        sap_observation_id=evaluation_item.sap_observation_id,
        sap_document_number=evaluation_item.sap_document_number,
        sap_line_item=evaluation_item.sap_line_item,
        amount=evaluation_item.amount,
        currency=evaluation_item.currency,
        snapshot_id=evaluation_item.snapshot_id,
        exact_evidence_link_id=evaluation_item.exact_evidence_link_id,
        versions=versions,
        semantic_label=judgment.semantic_label,
        confidence_tier=judgment.confidence_tier,
        evidence_refs=refs,
        recommended_account_ids=judgment.recommended_account_ids,
        rationale_summary=judgment.rationale_summary,
        missing_evidence=judgment.missing_evidence,
        detected_at=datetime.now(timezone.utc),
    )


def _require_uncertainty_wording(value: str) -> None:
    if any(word in value for word in ("确定", "必然", "一定", "毫无疑问")) or not any(
        word in value for word in ("可能", "疑似", "现有证据", "建议", "倾向")
    ):
        raise BusinessEvidenceReviewError("rationale must use professional uncertainty wording")


__all__ = [
    "BusinessEntertainmentEvidencePack",
    "BusinessEvidenceField",
    "BusinessEvidenceReviewError",
    "build_business_evidence_pack",
    "review_and_assemble_detection",
]

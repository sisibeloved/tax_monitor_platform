"""Build immutable SAP evidence packs and resolve exact model citations."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tax_risk.domain.semantic.contracts import (
    EvidenceRef,
    SemanticModelJudgment,
    SemanticVersionSet,
)
from tax_risk.domain.semantic.sap_voucher import SnapshotBoundSapExpenseVoucher


class CitationResolutionError(ValueError):
    pass


class EvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=64, max_length=64)
    field_name: str
    value: str


class EvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_record_id: UUID
    snapshot_id: UUID
    observation_id: UUID
    versions: SemanticVersionSet
    fields: tuple[EvidenceField, ...]


_SAP_EVIDENCE_FIELDS = (
    "posting_date",
    "current_account_code",
    "current_account_name",
    "summary",
    "assignment",
    "reference",
    "reversal_reference",
)


def build_sap_voucher_evidence_pack(
    view: SnapshotBoundSapExpenseVoucher,
    versions: SemanticVersionSet,
) -> EvidencePack:
    fields: list[EvidenceField] = []
    for field_name in _SAP_EVIDENCE_FIELDS:
        raw_value = getattr(view, field_name)
        value = "" if raw_value is None else str(raw_value)
        identifier = sha256(
            "\0".join(
                (
                    str(view.snapshot_id),
                    str(view.source_record_id),
                    field_name,
                    value,
                    versions.rule_version_id,
                )
            ).encode()
        ).hexdigest()
        fields.append(
            EvidenceField(
                evidence_id=identifier,
                field_name=field_name,
                value=value,
            )
        )
    return EvidencePack(
        source_record_id=view.source_record_id,
        snapshot_id=view.snapshot_id,
        observation_id=view.observation_id,
        versions=versions,
        fields=tuple(fields),
    )


def resolve_citations(
    judgment: SemanticModelJudgment,
    evidence_pack: EvidencePack,
) -> list[EvidenceRef]:
    by_id = {field.evidence_id: field for field in evidence_pack.fields}
    resolved: list[EvidenceRef] = []
    seen: set[tuple[str, str, str]] = set()
    for citation in judgment.evidence_citations:
        field = by_id.get(citation.evidence_id)
        if field is None:
            raise CitationResolutionError("citation references evidence outside the pack")
        if field.field_name != citation.field_name:
            raise CitationResolutionError("citation field does not match its evidence")
        if citation.quoted_text not in field.value:
            raise CitationResolutionError("citation quote is not present in evidence")
        key = (citation.evidence_id, citation.field_name, citation.quoted_text)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(
            EvidenceRef(
                evidence_id=citation.evidence_id,
                field_name=citation.field_name,
                quoted_text=citation.quoted_text,
                source_record_id=evidence_pack.source_record_id,
                snapshot_id=evidence_pack.snapshot_id,
            )
        )
    return resolved


__all__ = [
    "CitationResolutionError",
    "EvidenceField",
    "EvidencePack",
    "build_sap_voucher_evidence_pack",
    "resolve_citations",
]

"""Deterministic exact-first linking for business-entertainment evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tax_risk.persistence.business_entertainment_models import EvidenceLink
from tax_risk.persistence.repositories import UnitOfWork


class EvidenceRelationQuality(StrEnum):
    EXACT = "EXACT"
    FUZZY = "FUZZY"


class SapEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    observation_id: UUID
    source_record_id: UUID
    snapshot_id: UUID
    company_code: str = Field(min_length=1, max_length=64)
    document_number: str = Field(min_length=1, max_length=64)
    line_item: str = Field(min_length=1, max_length=32)
    posting_date: date
    amount: Decimal
    assignment: str | None = None
    reference: str | None = None

    @property
    def sap_key(self) -> str:
        return "|".join((self.company_code, self.document_number, self.line_item))


class BusinessEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_record_id: UUID
    dataset_code: str
    company_code: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    document_date: date
    amount: Decimal | None
    related_oa_id: str | None = None
    sap_document_number: str | None = None
    sap_line_item: str | None = None
    parent_oa_id: str | None = None
    parent_hesi_id: str | None = None

    @model_validator(mode="after")
    def validate_direct_sap_pair(self) -> BusinessEvidence:
        if (self.sap_document_number is None) != (self.sap_line_item is None):
            raise ValueError("direct SAP document and line must be supplied together")
        return self

    @property
    def business_key(self) -> str:
        return "|".join((self.company_code, self.dataset_code, self.document_id, self.line_id))

    @property
    def is_hesi(self) -> bool:
        return self.dataset_code == "hesi_business_entertainment"

    @property
    def is_oa(self) -> bool:
        return self.dataset_code == "oa_business_entertainment"

    @property
    def is_child(self) -> bool:
        return self.dataset_code in {"oa_self_procurement", "oa_material_requisition"}


class EvidenceLinkDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_code: str
    source_record_id: UUID
    target_record_id: UUID
    relation_kind: str
    relation_quality: EvidenceRelationQuality
    matched_field: str
    snapshot_id: UUID | None


class LinkConflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_code: str
    subject_key: str
    reason: str
    reference: str


class LinkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exact_links: tuple[EvidenceLinkDecision, ...]
    fuzzy_hints: tuple[EvidenceLinkDecision, ...]
    conflicts: tuple[LinkConflict, ...]
    unmatched_sap_keys: tuple[str, ...]
    unmatched_canonical_business_keys: tuple[str, ...]


def _link_key(link: EvidenceLinkDecision) -> tuple[str, str, str, str]:
    return (
        str(link.source_record_id),
        str(link.target_record_id),
        link.relation_kind,
        link.matched_field,
    )


def link_exact_evidence(
    sap_evidence: tuple[SapEvidence, ...],
    business_evidence: tuple[BusinessEvidence, ...],
    *,
    snapshot_id: UUID | None = None,
) -> LinkResult:
    """Link only controlled exact identifiers; similarity remains a non-evidence hint."""

    saps = tuple(sorted(sap_evidence, key=lambda item: (item.sap_key, str(item.observation_id))))
    businesses = tuple(
        sorted(business_evidence, key=lambda item: (item.business_key, str(item.source_record_id)))
    )
    snapshot_ids = {item.snapshot_id for item in saps}
    resolved_snapshot_id = snapshot_id or (next(iter(snapshot_ids)) if len(snapshot_ids) == 1 else None)

    by_company_document: dict[tuple[str, str], list[BusinessEvidence]] = defaultdict(list)
    by_company_sap_key: dict[tuple[str, str, str], list[BusinessEvidence]] = defaultdict(list)
    for business in businesses:
        by_company_document[(business.company_code, business.document_id)].append(business)
        if business.sap_document_number and business.sap_line_item:
            by_company_sap_key[
                (business.company_code, business.sap_document_number, business.sap_line_item)
            ].append(business)

    exact: list[EvidenceLinkDecision] = []
    fuzzy: list[EvidenceLinkDecision] = []
    conflicts: list[LinkConflict] = []
    linked_sap_ids: set[UUID] = set()
    linked_business_ids: set[UUID] = set()
    oa_to_hesi: dict[UUID, BusinessEvidence] = {}

    # Establish business-document hierarchy first so Hesi is the canonical record.
    for hesi in (item for item in businesses if item.is_hesi and item.related_oa_id):
        matches = by_company_document.get((hesi.company_code, hesi.related_oa_id or ""), [])
        oa_matches = [item for item in matches if item.is_oa]
        if len(oa_matches) == 1:
            oa = oa_matches[0]
            oa_to_hesi[oa.source_record_id] = hesi
            exact.append(
                EvidenceLinkDecision(
                    company_code=hesi.company_code,
                    source_record_id=hesi.source_record_id,
                    target_record_id=oa.source_record_id,
                    relation_kind="HESI_TO_OA",
                    relation_quality=EvidenceRelationQuality.EXACT,
                    matched_field="related_oa_id",
                    snapshot_id=resolved_snapshot_id,
                )
            )
            linked_business_ids.add(oa.source_record_id)
        elif len(oa_matches) > 1:
            conflicts.append(
                LinkConflict(
                    company_code=hesi.company_code,
                    subject_key=hesi.business_key,
                    reason="AMBIGUOUS_EXACT_REFERENCE",
                    reference=hesi.related_oa_id or "",
                )
            )

    for child in (item for item in businesses if item.is_child):
        parent_field = "parent_hesi_id" if child.parent_hesi_id else "parent_oa_id"
        parent_id = child.parent_hesi_id or child.parent_oa_id or ""
        matches = by_company_document.get((child.company_code, parent_id), [])
        expected = [
            item
            for item in matches
            if (parent_field == "parent_hesi_id" and item.is_hesi)
            or (parent_field == "parent_oa_id" and item.is_oa)
        ]
        if len(expected) == 1:
            parent = expected[0]
            exact.append(
                EvidenceLinkDecision(
                    company_code=child.company_code,
                    source_record_id=child.source_record_id,
                    target_record_id=parent.source_record_id,
                    relation_kind="CHILD_TO_HESI" if parent.is_hesi else "CHILD_TO_OA",
                    relation_quality=EvidenceRelationQuality.EXACT,
                    matched_field=parent_field,
                    snapshot_id=resolved_snapshot_id,
                )
            )
            linked_business_ids.add(child.source_record_id)
        elif len(expected) > 1:
            conflicts.append(
                LinkConflict(
                    company_code=child.company_code,
                    subject_key=child.business_key,
                    reason="AMBIGUOUS_EXACT_REFERENCE",
                    reference=parent_id,
                )
            )

    for sap in saps:
        candidates = by_company_sap_key.get(
            (sap.company_code, sap.document_number, sap.line_item), []
        )
        matched_field = "sap_document_number+sap_line_item"
        reference_value = ""
        if not candidates:
            references = tuple(value for value in (sap.assignment, sap.reference) if value)
            candidate_map: dict[UUID, BusinessEvidence] = {}
            for reference in references:
                reference_value = reference
                for item in by_company_document.get((sap.company_code, reference), []):
                    canonical_candidate = oa_to_hesi.get(item.source_record_id, item)
                    if not canonical_candidate.is_child:
                        candidate_map[canonical_candidate.source_record_id] = canonical_candidate
            candidates = list(candidate_map.values())
            matched_field = "assignment/reference"

        if len(candidates) == 1:
            business = candidates[0]
            exact.append(
                EvidenceLinkDecision(
                    company_code=sap.company_code,
                    source_record_id=business.source_record_id,
                    target_record_id=sap.source_record_id,
                    relation_kind="BUSINESS_TO_SAP",
                    relation_quality=EvidenceRelationQuality.EXACT,
                    matched_field=matched_field,
                    snapshot_id=sap.snapshot_id,
                )
            )
            linked_sap_ids.add(sap.observation_id)
            linked_business_ids.add(business.source_record_id)
        elif len(candidates) > 1:
            conflicts.append(
                LinkConflict(
                    company_code=sap.company_code,
                    subject_key=sap.sap_key,
                    reason="AMBIGUOUS_EXACT_REFERENCE",
                    reference=reference_value or f"{sap.document_number}|{sap.line_item}",
                )
            )
        else:
            cross_values = {value for value in (sap.assignment, sap.reference) if value}
            cross = any(
                item.document_id in cross_values and item.company_code != sap.company_code
                for item in businesses
            )
            if cross:
                conflicts.append(
                    LinkConflict(
                        company_code=sap.company_code,
                        subject_key=sap.sap_key,
                        reason="CROSS_COMPANY_REFERENCE",
                        reference=sorted(cross_values)[0],
                    )
                )
            else:
                similar = [
                    item
                    for item in businesses
                    if not item.is_child
                    and item.company_code == sap.company_code
                    and item.amount == sap.amount
                    and item.document_date == sap.posting_date
                ]
                for item in similar:
                    fuzzy.append(
                        EvidenceLinkDecision(
                            company_code=sap.company_code,
                            source_record_id=item.source_record_id,
                            target_record_id=sap.source_record_id,
                            relation_kind="BUSINESS_TO_SAP_HINT",
                            relation_quality=EvidenceRelationQuality.FUZZY,
                            matched_field="amount+date",
                            snapshot_id=sap.snapshot_id,
                        )
                    )

    canonical_records = [item for item in businesses if item.is_hesi or item.is_oa]
    canonical_records = [
        item for item in canonical_records if item.source_record_id not in oa_to_hesi
    ]
    return LinkResult(
        exact_links=tuple(sorted(exact, key=_link_key)),
        fuzzy_hints=tuple(sorted(fuzzy, key=_link_key)),
        conflicts=tuple(
            sorted(conflicts, key=lambda item: (item.subject_key, item.reason, item.reference))
        ),
        unmatched_sap_keys=tuple(
            item.sap_key for item in saps if item.observation_id not in linked_sap_ids
        ),
        unmatched_canonical_business_keys=tuple(
            item.business_key
            for item in canonical_records
            if item.source_record_id not in linked_business_ids
        ),
    )


class ExactEvidenceLinker:
    """Persist only deterministic EXACT relations within the caller snapshot."""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def link_and_persist(
        self,
        sap_evidence: tuple[SapEvidence, ...],
        business_evidence: tuple[BusinessEvidence, ...],
        *,
        snapshot_id: UUID,
    ) -> LinkResult:
        result = link_exact_evidence(
            sap_evidence,
            business_evidence,
            snapshot_id=snapshot_id,
        )
        with self._uow_factory() as uow:
            for decision in result.exact_links:
                if decision.snapshot_id != snapshot_id:
                    raise ValueError("all exact relations must belong to the requested snapshot")
                uow.business_entertainment_scope.add_evidence_link(
                    EvidenceLink(
                        company_code=decision.company_code,
                        source_record_id=decision.source_record_id,
                        target_record_id=decision.target_record_id,
                        relation_kind=decision.relation_kind,
                        relation_quality=EvidenceRelationQuality.EXACT.value,
                        matched_field=decision.matched_field,
                        snapshot_id=snapshot_id,
                    )
                )
            uow.commit()
        return result


__all__ = [
    "BusinessEvidence",
    "ExactEvidenceLinker",
    "EvidenceLinkDecision",
    "EvidenceRelationQuality",
    "LinkConflict",
    "LinkResult",
    "SapEvidence",
    "link_exact_evidence",
]

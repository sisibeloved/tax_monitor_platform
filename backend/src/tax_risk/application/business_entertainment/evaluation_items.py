"""Build deterministic two-path evaluation and independent SAP coverage items."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tax_risk.domain.business_entertainment.evaluation import (
    AmountSource,
    BusinessEntertainmentEvaluationItem,
    CanonicalRecordType,
    EvaluationSourceMode,
    SapLinkCoverageItem,
    SapLinkStatus,
)


class EvaluationBuildError(Exception):
    pass


class SapEvaluationSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    observation_id: UUID
    source_record_id: UUID
    snapshot_id: UUID
    snapshot_period_end: date
    company_code: str = Field(min_length=1, max_length=64)
    fiscal_year: int = Field(ge=2000, le=9999)
    period: int = Field(ge=1, le=12)
    posting_date: date
    document_number: str = Field(min_length=1, max_length=64)
    line_item: str = Field(min_length=1, max_length=32)
    current_account_code: str = Field(min_length=1, max_length=64)
    current_account_name: str = Field(min_length=1, max_length=256)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def posting_period_matches(self) -> SapEvaluationSource:
        if self.posting_date.year != self.fiscal_year or self.posting_date.month != self.period:
            raise ValueError("posting date must match fiscal year and period")
        if not self.amount.is_finite():
            raise ValueError("amount must be finite")
        return self

    @property
    def business_key(self) -> str:
        return "|".join(
            (self.company_code, str(self.fiscal_year), self.document_number, self.line_item)
        )


class BusinessEvaluationSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_record_id: UUID
    dataset_code: str
    company_code: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    document_date: date
    amount: Decimal | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def amount_pair_is_consistent(self) -> BusinessEvaluationSource:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency must be supplied together")
        if self.amount is not None and not self.amount.is_finite():
            raise ValueError("amount must be finite")
        return self

    @property
    def business_key(self) -> str:
        return "|".join(
            (self.company_code, self.dataset_code, self.document_id, self.line_id)
        )


class ExactEvidenceRelation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    evidence_link_id: UUID
    company_code: str = Field(min_length=1, max_length=64)
    source_record_id: UUID
    target_record_id: UUID
    relation_kind: str
    snapshot_id: UUID


def _canonical_type(source: BusinessEvaluationSource) -> CanonicalRecordType | None:
    if source.dataset_code == "hesi_business_entertainment":
        return CanonicalRecordType.HESI
    if source.dataset_code == "oa_business_entertainment":
        return CanonicalRecordType.OA
    return None


def _assert_snapshot(
    snapshot_id: UUID,
    sap_sources: tuple[SapEvaluationSource, ...],
    relations: tuple[ExactEvidenceRelation, ...],
) -> None:
    if any(source.snapshot_id != snapshot_id for source in sap_sources):
        raise EvaluationBuildError("SAP source belongs to another snapshot")
    if any(relation.snapshot_id != snapshot_id for relation in relations):
        raise EvaluationBuildError("evidence relation belongs to another snapshot")


def build_evaluation_items(
    snapshot_id: UUID,
    sap_sources: tuple[SapEvaluationSource, ...],
    business_sources: tuple[BusinessEvaluationSource, ...],
    exact_relations: tuple[ExactEvidenceRelation, ...],
) -> tuple[BusinessEntertainmentEvaluationItem, ...]:
    _assert_snapshot(snapshot_id, sap_sources, exact_relations)
    sap_by_source_id = {source.source_record_id: source for source in sap_sources}
    business_by_source_id = {source.source_record_id: source for source in business_sources}

    suppressed_oa_ids: set[UUID] = set()
    business_chain_relation: dict[UUID, ExactEvidenceRelation] = {}
    sap_relation_by_business: dict[UUID, ExactEvidenceRelation] = {}
    for relation in exact_relations:
        source = business_by_source_id.get(relation.source_record_id)
        if source is None or source.company_code != relation.company_code:
            raise EvaluationBuildError("evidence source is missing or crosses company boundary")
        if relation.relation_kind == "HESI_TO_OA":
            target = business_by_source_id.get(relation.target_record_id)
            if target is None or target.company_code != relation.company_code:
                raise EvaluationBuildError("Hesi/OA evidence target is invalid")
            suppressed_oa_ids.add(target.source_record_id)
            business_chain_relation[source.source_record_id] = relation
        elif relation.relation_kind == "BUSINESS_TO_SAP":
            target_sap = sap_by_source_id.get(relation.target_record_id)
            if target_sap is None or target_sap.company_code != relation.company_code:
                raise EvaluationBuildError("business/SAP evidence target is invalid")
            if source.source_record_id in sap_relation_by_business:
                raise EvaluationBuildError("canonical business document has duplicate SAP links")
            sap_relation_by_business[source.source_record_id] = relation

    items: list[BusinessEntertainmentEvaluationItem] = []
    for business in sorted(business_sources, key=lambda item: item.business_key):
        canonical_type = _canonical_type(business)
        if canonical_type is None or business.source_record_id in suppressed_oa_ids:
            continue
        if business.amount is None or business.currency is None:
            raise EvaluationBuildError("canonical business document amount is missing")

        sap_relation = sap_relation_by_business.get(business.source_record_id)
        if sap_relation is not None:
            sap = sap_by_source_id[sap_relation.target_record_id]
            item = BusinessEntertainmentEvaluationItem(
                candidate_key=f"{snapshot_id}|SAP_LINKED|{business.source_record_id}",
                company_code=business.company_code,
                fiscal_year=sap.fiscal_year,
                period=sap.period,
                source_mode=EvaluationSourceMode.SAP_LINKED,
                canonical_record_type=canonical_type,
                canonical_source_record_id=business.source_record_id,
                canonical_business_key=business.business_key,
                sap_observation_id=sap.observation_id,
                sap_business_key=sap.business_key,
                sap_document_number=sap.document_number,
                sap_line_item=sap.line_item,
                current_account_code=sap.current_account_code,
                current_account_name=sap.current_account_name,
                amount=sap.amount,
                currency=sap.currency,
                amount_source=AmountSource.SAP,
                exact_evidence_link_id=sap_relation.evidence_link_id,
                snapshot_id=snapshot_id,
            )
        else:
            item = BusinessEntertainmentEvaluationItem(
                candidate_key=f"{snapshot_id}|BUSINESS_UNLINKED|{business.source_record_id}",
                company_code=business.company_code,
                fiscal_year=business.document_date.year,
                period=business.document_date.month,
                source_mode=EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED,
                canonical_record_type=canonical_type,
                canonical_source_record_id=business.source_record_id,
                canonical_business_key=business.business_key,
                sap_observation_id=None,
                sap_business_key=None,
                sap_document_number=None,
                sap_line_item=None,
                current_account_code=None,
                current_account_name=None,
                amount=business.amount,
                currency=business.currency,
                amount_source=(
                    AmountSource.HESI
                    if canonical_type is CanonicalRecordType.HESI
                    else AmountSource.OA
                ),
                exact_evidence_link_id=(
                    business_chain_relation[business.source_record_id].evidence_link_id
                    if business.source_record_id in business_chain_relation
                    else None
                ),
                snapshot_id=snapshot_id,
            )
        items.append(item)
    return tuple(items)


def build_sap_coverage_items(
    snapshot_id: UUID,
    sap_sources: tuple[SapEvaluationSource, ...],
    exact_relations: tuple[ExactEvidenceRelation, ...],
) -> tuple[SapLinkCoverageItem, ...]:
    _assert_snapshot(snapshot_id, sap_sources, exact_relations)
    sap_relations: dict[UUID, ExactEvidenceRelation] = {}
    for relation in exact_relations:
        if relation.relation_kind != "BUSINESS_TO_SAP":
            continue
        if relation.target_record_id in sap_relations:
            raise EvaluationBuildError("SAP row has duplicate exact business links")
        sap_relations[relation.target_record_id] = relation

    items: list[SapLinkCoverageItem] = []
    for sap in sorted(sap_sources, key=lambda item: item.business_key):
        coverage_relation = sap_relations.get(sap.source_record_id)
        items.append(
            SapLinkCoverageItem(
                company_code=sap.company_code,
                period_end=sap.snapshot_period_end,
                sap_observation_id=sap.observation_id,
                document_number=sap.document_number,
                line_item=sap.line_item,
                amount=sap.amount,
                currency=sap.currency,
                link_status=(
                    SapLinkStatus.LINKED
                    if coverage_relation is not None
                    else SapLinkStatus.UNLINKED
                ),
                exact_evidence_link_id=(
                    coverage_relation.evidence_link_id
                    if coverage_relation is not None
                    else None
                ),
                evaluated_via_business_document=coverage_relation is not None,
                snapshot_id=snapshot_id,
            )
        )
    return tuple(items)


__all__ = [
    "BusinessEvaluationSource",
    "EvaluationBuildError",
    "ExactEvidenceRelation",
    "SapEvaluationSource",
    "build_evaluation_items",
    "build_sap_coverage_items",
]

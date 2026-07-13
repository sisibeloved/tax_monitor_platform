from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from tax_risk.application.business_entertainment.evaluation_items import (
    BusinessEvaluationSource,
    ExactEvidenceRelation,
    SapEvaluationSource,
    build_evaluation_items,
    build_sap_coverage_items,
)
from tax_risk.domain.business_entertainment.evaluation import (
    AmountSource,
    BusinessEntertainmentEvaluationItem,
    CanonicalRecordType,
    EvaluationSourceMode,
    SapLinkStatus,
)


SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000099")


def _sap(**overrides: object) -> SapEvaluationSource:
    values: dict[str, object] = {
        "observation_id": uuid4(),
        "source_record_id": uuid4(),
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_period_end": date(2026, 6, 30),
        "company_code": "C001",
        "fiscal_year": 2026,
        "period": 3,
        "posting_date": date(2026, 3, 18),
        "document_number": "510001",
        "line_item": "001",
        "current_account_code": "660201",
        "current_account_name": "业务招待费",
        "amount": Decimal("120.50"),
        "currency": "CNY",
    }
    values.update(overrides)
    return SapEvaluationSource(**values)  # type: ignore[arg-type]


def _business(
    dataset_code: str = "hesi_business_entertainment",
    **overrides: object,
) -> BusinessEvaluationSource:
    values: dict[str, object] = {
        "source_record_id": uuid4(),
        "dataset_code": dataset_code,
        "company_code": "C001",
        "document_id": "HESI-1",
        "line_id": "1",
        "document_date": date(2026, 3, 17),
        "amount": Decimal("120.50"),
        "currency": "CNY",
    }
    values.update(overrides)
    return BusinessEvaluationSource(**values)  # type: ignore[arg-type]


def _relation(
    source_record_id: UUID,
    target_record_id: UUID,
    relation_kind: str,
) -> ExactEvidenceRelation:
    return ExactEvidenceRelation(
        evidence_link_id=uuid4(),
        company_code="C001",
        source_record_id=source_record_id,
        target_record_id=target_record_id,
        relation_kind=relation_kind,
        snapshot_id=SNAPSHOT_ID,
    )


def test_exact_business_to_sap_chain_builds_one_sap_linked_item() -> None:
    sap = _sap()
    hesi = _business()
    relation = _relation(hesi.source_record_id, sap.source_record_id, "BUSINESS_TO_SAP")

    items = build_evaluation_items(SNAPSHOT_ID, (sap,), (hesi,), (relation,))

    assert len(items) == 1
    item = items[0]
    assert item.source_mode is EvaluationSourceMode.SAP_LINKED
    assert item.canonical_record_type is CanonicalRecordType.HESI
    assert item.sap_observation_id == sap.observation_id
    assert item.sap_document_number == sap.document_number
    assert item.sap_line_item == sap.line_item
    assert item.current_account_code == "660201"
    assert item.amount == Decimal("120.50")
    assert item.amount_source is AmountSource.SAP
    assert item.exact_evidence_link_id == relation.evidence_link_id


def test_hesi_to_oa_chain_without_sap_uses_hesi_as_one_unlinked_canonical_item() -> None:
    hesi = _business(document_id="H-1")
    oa = _business("oa_business_entertainment", document_id="OA-1")
    relation = _relation(hesi.source_record_id, oa.source_record_id, "HESI_TO_OA")

    items = build_evaluation_items(SNAPSHOT_ID, (), (oa, hesi), (relation,))

    assert len(items) == 1
    item = items[0]
    assert item.source_mode is EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED
    assert item.canonical_record_type is CanonicalRecordType.HESI
    assert item.canonical_source_record_id == hesi.source_record_id
    assert item.amount_source is AmountSource.HESI
    assert item.exact_evidence_link_id == relation.evidence_link_id
    assert item.sap_observation_id is None


def test_independent_hesi_and_oa_each_build_unlinked_item() -> None:
    hesi = _business(document_id="H-1")
    oa = _business("oa_business_entertainment", document_id="OA-1")

    items = build_evaluation_items(SNAPSHOT_ID, (), (oa, hesi), ())

    assert {item.canonical_record_type for item in items} == {
        CanonicalRecordType.HESI,
        CanonicalRecordType.OA,
    }
    assert all(
        item.source_mode is EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED
        for item in items
    )


def test_self_procurement_and_material_only_never_become_canonical_items() -> None:
    self_procurement = _business("oa_self_procurement", document_id="SELF-1")
    material = _business(
        "oa_material_requisition",
        document_id="MAT-1",
        amount=None,
        currency=None,
    )

    items = build_evaluation_items(
        SNAPSHOT_ID,
        (),
        (self_procurement, material),
        (),
    )

    assert items == ()


def test_independent_sap_only_builds_unlinked_coverage_not_evaluation_item() -> None:
    sap = _sap()

    items = build_evaluation_items(SNAPSHOT_ID, (sap,), (), ())
    coverages = build_sap_coverage_items(SNAPSHOT_ID, (sap,), ())

    assert items == ()
    assert len(coverages) == 1
    assert coverages[0].link_status is SapLinkStatus.UNLINKED
    assert coverages[0].evaluated_via_business_document is False
    assert coverages[0].exact_evidence_link_id is None


def test_coverage_marks_exact_sap_link_and_carries_auditable_amount_fields() -> None:
    sap = _sap()
    hesi = _business()
    relation = _relation(hesi.source_record_id, sap.source_record_id, "BUSINESS_TO_SAP")

    coverages = build_sap_coverage_items(SNAPSHOT_ID, (sap,), (relation,))

    assert len(coverages) == 1
    coverage = coverages[0]
    assert coverage.link_status is SapLinkStatus.LINKED
    assert coverage.exact_evidence_link_id == relation.evidence_link_id
    assert coverage.evaluated_via_business_document is True
    assert coverage.document_number == sap.document_number
    assert coverage.line_item == sap.line_item
    assert coverage.amount == sap.amount
    assert coverage.period_end == date(2026, 6, 30)


def test_unlinked_mode_rejects_any_sap_only_field() -> None:
    with pytest.raises(ValidationError, match="SAP fields"):
        BusinessEntertainmentEvaluationItem(
            candidate_key="candidate",
            company_code="C001",
            fiscal_year=2026,
            period=3,
            source_mode=EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED,
            canonical_record_type=CanonicalRecordType.HESI,
            canonical_source_record_id=uuid4(),
            canonical_business_key="C001|HESI-1|1",
            sap_observation_id=None,
            sap_business_key=None,
            sap_document_number="510001",
            sap_line_item=None,
            current_account_code=None,
            current_account_name=None,
            amount=Decimal("120.50"),
            currency="CNY",
            amount_source=AmountSource.HESI,
            exact_evidence_link_id=None,
            snapshot_id=SNAPSHOT_ID,
        )

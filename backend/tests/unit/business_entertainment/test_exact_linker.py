from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from hypothesis import given
from hypothesis import strategies as st

from tax_risk.application.business_entertainment.linker import (
    BusinessEvidence,
    ExactEvidenceLinker,
    EvidenceRelationQuality,
    SapEvidence,
    link_exact_evidence,
)


def _sap(**overrides: object) -> SapEvidence:
    values: dict[str, object] = {
        "observation_id": uuid4(),
        "source_record_id": uuid4(),
        "snapshot_id": uuid4(),
        "company_code": "C001",
        "document_number": "51000001",
        "line_item": "001",
        "posting_date": date(2026, 3, 15),
        "amount": Decimal("100.00"),
        "assignment": None,
        "reference": None,
    }
    values.update(overrides)
    return SapEvidence(**values)  # type: ignore[arg-type]


def _business(dataset_code: str = "hesi_business_entertainment", **overrides: object) -> BusinessEvidence:
    values: dict[str, object] = {
        "source_record_id": uuid4(),
        "dataset_code": dataset_code,
        "company_code": "C001",
        "document_id": "HESI-001",
        "line_id": "1",
        "document_date": date(2026, 3, 15),
        "amount": Decimal("100.00"),
        "related_oa_id": None,
        "sap_document_number": None,
        "sap_line_item": None,
        "parent_oa_id": None,
        "parent_hesi_id": None,
    }
    values.update(overrides)
    return BusinessEvidence(**values)  # type: ignore[arg-type]


def test_direct_sap_document_and_line_are_exact() -> None:
    sap = _sap()
    hesi = _business(
        sap_document_number=sap.document_number,
        sap_line_item=sap.line_item,
    )

    result = link_exact_evidence((sap,), (hesi,))

    assert len(result.exact_links) == 1
    assert result.exact_links[0].relation_quality is EvidenceRelationQuality.EXACT
    assert result.exact_links[0].matched_field == "sap_document_number+sap_line_item"
    assert result.unmatched_sap_keys == ()


def test_sap_assignment_or_reference_exact_id_links_to_hesi_canonical_over_oa() -> None:
    oa = _business("oa_business_entertainment", document_id="OA-9")
    hesi = _business(document_id="HESI-9", related_oa_id="OA-9")
    sap = _sap(assignment="OA-9")

    result = link_exact_evidence((sap,), (oa, hesi))

    sap_links = [item for item in result.exact_links if item.relation_kind == "BUSINESS_TO_SAP"]
    assert len(sap_links) == 1
    assert sap_links[0].source_record_id == hesi.source_record_id
    assert any(item.relation_kind == "HESI_TO_OA" for item in result.exact_links)


def test_self_procurement_and_material_requisition_link_only_to_exact_parent() -> None:
    hesi = _business(document_id="H-1")
    child = _business(
        "oa_self_procurement",
        document_id="SELF-1",
        parent_hesi_id="H-1",
    )
    material = _business(
        "oa_material_requisition",
        document_id="MAT-1",
        amount=None,
        parent_hesi_id="H-1",
    )

    result = link_exact_evidence((), (hesi, child, material))

    assert [link.relation_kind for link in result.exact_links].count("CHILD_TO_HESI") == 2
    assert result.unmatched_canonical_business_keys == (hesi.business_key,)


def test_cross_company_and_duplicate_reference_are_conflicts_not_exact_links() -> None:
    cross_company = _business(company_code="C002", document_id="REF-1")
    duplicate_a = _business(document_id="DUP")
    duplicate_b = _business(document_id="DUP", line_id="2")
    sap_cross = _sap(reference="REF-1")
    sap_duplicate = _sap(document_number="51000002", reference="DUP")

    result = link_exact_evidence(
        (sap_cross, sap_duplicate),
        (cross_company, duplicate_a, duplicate_b),
    )

    assert not result.exact_links
    assert {conflict.reason for conflict in result.conflicts} == {
        "CROSS_COMPANY_REFERENCE",
        "AMBIGUOUS_EXACT_REFERENCE",
    }


def test_amount_and_date_only_produce_fuzzy_hint_never_evidence() -> None:
    sap = _sap()
    business = _business(document_id="NO-ID-MATCH")

    result = link_exact_evidence((sap,), (business,))

    assert not result.exact_links
    assert len(result.fuzzy_hints) == 1
    assert result.fuzzy_hints[0].relation_quality is EvidenceRelationQuality.FUZZY
    assert result.unmatched_sap_keys == (sap.sap_key,)
    assert result.unmatched_canonical_business_keys == (business.business_key,)


def test_persistence_writes_exact_relations_but_never_fuzzy_hints() -> None:
    exact_sap = _sap(reference="EXACT", amount=Decimal("10"))
    fuzzy_sap = _sap(
        observation_id=uuid4(),
        source_record_id=uuid4(),
        document_number="51000002",
        amount=Decimal("20"),
    )
    exact_business = _business(document_id="EXACT", amount=Decimal("10"))
    fuzzy_business = _business(
        source_record_id=uuid4(),
        document_id="NO-REFERENCE",
        amount=Decimal("20"),
    )
    persisted: list[object] = []

    class FakeRepository:
        def add_evidence_link(self, link: object) -> None:
            persisted.append(link)

    class FakeUow:
        business_entertainment_scope = FakeRepository()

        def __enter__(self) -> FakeUow:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def commit(self) -> None:
            return None

    result = ExactEvidenceLinker(lambda: FakeUow()).link_and_persist(  # type: ignore[arg-type]
        (exact_sap, fuzzy_sap),
        (exact_business, fuzzy_business),
        snapshot_id=exact_sap.snapshot_id,
    )

    assert len(result.exact_links) == len(persisted) == 1
    assert len(result.fuzzy_hints) == 1
    assert getattr(persisted[0], "relation_quality") == "EXACT"


@given(st.permutations((0, 1, 2)))
def test_link_result_is_input_order_invariant(order: list[int]) -> None:
    snapshot_id = UUID("00000000-0000-0000-0000-000000000099")
    sap = _sap(
        observation_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_record_id=UUID("00000000-0000-0000-0000-000000000002"),
        snapshot_id=snapshot_id,
        reference="H-1",
    )
    records = (
        _business(source_record_id=UUID("00000000-0000-0000-0000-000000000010"), document_id="H-1"),
        _business(
            "oa_business_entertainment",
            source_record_id=UUID("00000000-0000-0000-0000-000000000011"),
            document_id="OA-1",
        ),
        _business(
            "oa_self_procurement",
            source_record_id=UUID("00000000-0000-0000-0000-000000000012"),
            document_id="S-1",
            parent_hesi_id="H-1",
        ),
    )

    expected = link_exact_evidence((sap,), records)
    reordered = link_exact_evidence((sap,), tuple(records[index] for index in order))

    assert reordered == expected

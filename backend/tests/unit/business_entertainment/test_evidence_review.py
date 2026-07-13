from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from tax_risk.application.business_entertainment.evidence_review import (
    BusinessEvidenceReviewError,
    build_business_evidence_pack,
    review_and_assemble_detection,
)
from tax_risk.domain.business_entertainment.evaluation import (
    AmountSource,
    BusinessEntertainmentEvaluationItem,
    CanonicalRecordType,
    EvaluationSourceMode,
)
from tax_risk.domain.semantic.contracts import (
    ConfidenceTier,
    EvidenceCitation,
    SemanticLabel,
    SemanticModelJudgment,
    SemanticVersionSet,
)


def _versions() -> SemanticVersionSet:
    return SemanticVersionSet(
        rule_version_id="rule-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version="accounts-v1",
    )


def _item(*, linked: bool = True) -> BusinessEntertainmentEvaluationItem:
    source_id = uuid4()
    return BusinessEntertainmentEvaluationItem(
        candidate_key="candidate-1",
        company_code="C001",
        fiscal_year=2026,
        period=3,
        source_mode=(
            EvaluationSourceMode.SAP_LINKED
            if linked
            else EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED
        ),
        canonical_record_type=CanonicalRecordType.HESI,
        canonical_source_record_id=source_id,
        canonical_business_key="C001|HESI|R1|1",
        sap_observation_id=uuid4() if linked else None,
        sap_business_key="C001|2026|100001|001" if linked else None,
        sap_document_number="100001" if linked else None,
        sap_line_item="001" if linked else None,
        current_account_code="660203" if linked else None,
        current_account_name="业务招待费" if linked else None,
        amount=Decimal("100.00"),
        currency="CNY",
        amount_source=AmountSource.SAP if linked else AmountSource.HESI,
        exact_evidence_link_id=uuid4() if linked else None,
        snapshot_id=uuid4(),
    )


def _judgment(evidence_id: str, *, rationale: str = "现有证据显示可能应计入会议费。") -> SemanticModelJudgment:
    return SemanticModelJudgment(
        semantic_label=SemanticLabel.MEETING_EXPENSE,
        confidence_tier=ConfidenceTier.HIGH,
        evidence_citations=[
            EvidenceCitation(
                evidence_id=evidence_id,
                field_name="reason",
                quoted_text="内部会议餐",
            )
        ],
        recommended_account_ids=["MEETING_EXPENSE"],
        rationale_summary=rationale,
        missing_evidence=[],
    )


def test_review_uses_authoritative_source_mode_amount_and_versions() -> None:
    item = _item(linked=False)
    pack = build_business_evidence_pack(
        candidate_key=item.candidate_key,
        snapshot_id=item.snapshot_id,
        canonical_source_record_id=item.canonical_source_record_id,
        fields=(("reason", "内部会议餐", item.canonical_source_record_id),),
    )
    detection = review_and_assemble_detection(
        evaluation_item=item,
        judgment=_judgment(pack.fields[0].evidence_id),
        evidence_pack=pack,
        versions=_versions(),
        account_validator=lambda account_id, label: account_id == label,
    )

    assert detection.source_mode == "BUSINESS_DOCUMENT_UNLINKED"
    assert detection.sap_observation_id is None
    assert detection.amount == Decimal("100.00")
    assert detection.versions.account_dictionary_version == "accounts-v1"


def test_review_rejects_external_evidence_unsupported_account_and_deterministic_wording() -> None:
    item = _item()
    pack = build_business_evidence_pack(
        candidate_key=item.candidate_key,
        snapshot_id=item.snapshot_id,
        canonical_source_record_id=item.canonical_source_record_id,
        fields=(("reason", "内部会议餐", item.canonical_source_record_id),),
    )
    valid_id = pack.fields[0].evidence_id

    with pytest.raises(BusinessEvidenceReviewError, match="outside"):
        review_and_assemble_detection(
            evaluation_item=item,
            judgment=_judgment("external"),
            evidence_pack=pack,
            versions=_versions(),
            account_validator=lambda _account, _label: True,
        )
    with pytest.raises(BusinessEvidenceReviewError, match="compatible"):
        review_and_assemble_detection(
            evaluation_item=item,
            judgment=_judgment(valid_id),
            evidence_pack=pack,
            versions=_versions(),
            account_validator=lambda _account, _label: False,
        )
    with pytest.raises(BusinessEvidenceReviewError, match="uncertainty"):
        review_and_assemble_detection(
            evaluation_item=item,
            judgment=_judgment(valid_id, rationale="该事项确定属于会议费。"),
            evidence_pack=pack,
            versions=_versions(),
            account_validator=lambda _account, _label: True,
        )


def test_review_rejects_pack_identity_or_snapshot_tampering() -> None:
    item = _item()
    pack = build_business_evidence_pack(
        candidate_key=item.candidate_key,
        snapshot_id=uuid4(),
        canonical_source_record_id=item.canonical_source_record_id,
        fields=(("reason", "内部会议餐", item.canonical_source_record_id),),
    )
    with pytest.raises(BusinessEvidenceReviewError, match="identity"):
        review_and_assemble_detection(
            evaluation_item=item,
            judgment=_judgment(pack.fields[0].evidence_id),
            evidence_pack=pack,
            versions=_versions(),
            account_validator=lambda _account, _label: True,
        )

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tax_risk.domain.semantic.contracts import (
    ConfidenceTier,
    EvidenceCitation,
    EvidenceRef,
    SemanticDetection,
    SemanticLabel,
    SemanticModelJudgment,
    SemanticVersionSet,
)


@pytest.mark.parametrize(
    "server_owned_field",
    [
        "company_code",
        "sap_document_number",
        "amount",
        "snapshot_id",
        "model_version_id",
    ],
)
def test_model_judgment_rejects_server_owned_fields(server_owned_field: str) -> None:
    payload: dict[str, object] = {
        "semantic_label": "MEETING_EXPENSE",
        "confidence_tier": "HIGH",
        "evidence_citations": [
            {
                "evidence_id": "evidence-1",
                "field_name": "summary",
                "quoted_text": "内部会议餐",
            }
        ],
        "recommended_account_ids": ["MEETING_EXPENSE"],
        "rationale_summary": "现有资料显示该支出更符合会议费特征。",
        "missing_evidence": [],
        server_owned_field: "malicious-value",
    }

    with pytest.raises(ValidationError, match=server_owned_field):
        SemanticModelJudgment.model_validate(payload)


def test_semantic_detection_requires_all_server_owned_lineage_and_versions() -> None:
    versions = SemanticVersionSet(
        rule_version_id="rule-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version="accounts-v1",
    )
    judgment = SemanticModelJudgment(
        semantic_label=SemanticLabel.MEETING_EXPENSE,
        confidence_tier=ConfidenceTier.HIGH,
        evidence_citations=[
            EvidenceCitation(
                evidence_id="evidence-1",
                field_name="summary",
                quoted_text="内部会议餐",
            )
        ],
        recommended_account_ids=["MEETING_EXPENSE"],
        rationale_summary="现有资料显示该支出更符合会议费特征。",
        missing_evidence=[],
    )
    valid = {
        "detection_key": "candidate-1|model-v1",
        "candidate_key": "candidate-1",
        "company_code": "C001",
        "fiscal_year": 2026,
        "period": 3,
        "source_mode": "SAP_LINKED",
        "canonical_source_record_id": uuid4(),
        "sap_observation_id": uuid4(),
        "sap_document_number": "100001",
        "sap_line_item": "001",
        "amount": Decimal("100.00"),
        "currency": "CNY",
        "snapshot_id": uuid4(),
        "versions": versions,
        "semantic_label": judgment.semantic_label,
        "confidence_tier": judgment.confidence_tier,
        "evidence_refs": [
            EvidenceRef(
                evidence_id="evidence-1",
                field_name="summary",
                quoted_text="内部会议餐",
                source_record_id=uuid4(),
                snapshot_id=uuid4(),
            )
        ],
        "recommended_account_ids": judgment.recommended_account_ids,
        "rationale_summary": judgment.rationale_summary,
        "missing_evidence": judgment.missing_evidence,
        "detected_at": datetime.now(timezone.utc),
    }

    detection = SemanticDetection.model_validate(valid)
    assert detection.versions.account_dictionary_version == "accounts-v1"

    for required in (
        "company_code",
        "canonical_source_record_id",
        "snapshot_id",
        "versions",
        "evidence_refs",
    ):
        incomplete = valid.copy()
        incomplete.pop(required)
        with pytest.raises(ValidationError):
            SemanticDetection.model_validate(incomplete)


def test_detection_rejects_unlinked_mode_with_sap_identifiers() -> None:
    with pytest.raises(ValidationError, match="unlinked"):
        SemanticDetection.model_validate(
            {
                "detection_key": "key",
                "candidate_key": "candidate",
                "company_code": "C001",
                "fiscal_year": 2026,
                "period": 3,
                "source_mode": "BUSINESS_DOCUMENT_UNLINKED",
                "canonical_source_record_id": uuid4(),
                "sap_observation_id": uuid4(),
                "sap_document_number": "100001",
                "sap_line_item": "001",
                "amount": "20",
                "currency": "CNY",
                "snapshot_id": uuid4(),
                "versions": {
                    "rule_version_id": "r",
                    "model_version_id": "m",
                    "prompt_version_id": "p",
                    "case_library_version_id": "c",
                    "account_dictionary_version": "a",
                },
                "semantic_label": "MEETING_EXPENSE",
                "confidence_tier": "HIGH",
                "evidence_refs": [],
                "recommended_account_ids": ["MEETING_EXPENSE"],
                "rationale_summary": "可能应计入会议费。",
                "missing_evidence": [],
                "detected_at": datetime.now(timezone.utc),
            }
        )

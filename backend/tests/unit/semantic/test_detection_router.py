from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from tax_risk.application.semantic.detection_router import (
    RoutingOutcome,
    decide_detection_route,
)
from tax_risk.domain.semantic.contracts import SemanticDetection


def _detection(label: str) -> SemanticDetection:
    return SemanticDetection.model_validate(
        {
            "detection_key": f"candidate-1|model-v1|{label}",
            "candidate_key": "candidate-1",
            "company_code": "C001",
            "fiscal_year": 2026,
            "period": 3,
            "source_mode": "BUSINESS_DOCUMENT_UNLINKED",
            "canonical_source_record_id": uuid4(),
            "sap_observation_id": None,
            "sap_document_number": None,
            "sap_line_item": None,
            "amount": Decimal("100"),
            "currency": "CNY",
            "snapshot_id": uuid4(),
            "versions": {
                "rule_version_id": "rule-v1",
                "model_version_id": "model-v1",
                "prompt_version_id": "prompt-v1",
                "case_library_version_id": "cases-v1",
                "account_dictionary_version": "accounts-v1",
            },
            "semantic_label": label,
            "confidence_tier": "HIGH",
            "evidence_refs": [],
            "recommended_account_ids": ["MANUAL_REVIEW"],
            "rationale_summary": "现有证据显示可能存在科目错入。",
            "missing_evidence": ["接待对象"] if label == "INSUFFICIENT_EVIDENCE" else [],
            "detected_at": datetime.now(timezone.utc),
        }
    )


def test_router_selects_exactly_one_outcome() -> None:
    suspicious = frozenset({"MEETING_EXPENSE", "EMPLOYEE_EDUCATION", "EMPLOYEE_WELFARE"})

    assert (
        decide_detection_route(_detection("CURRENT_ACCOUNT_REASONABLE"), suspicious)
        is RoutingOutcome.DETECTION_ONLY
    )
    assert (
        decide_detection_route(_detection("INSUFFICIENT_EVIDENCE"), suspicious)
        is RoutingOutcome.EVIDENCE_TASK
    )
    assert (
        decide_detection_route(_detection("MEETING_EXPENSE"), suspicious)
        is RoutingOutcome.RISK_CASE
    )

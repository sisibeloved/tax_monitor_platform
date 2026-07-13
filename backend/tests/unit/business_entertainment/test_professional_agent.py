from __future__ import annotations

import asyncio
from datetime import date
from uuid import uuid4

import pytest

from tax_risk.adapters.model.fake_structured_client import FakeStructuredModelClient
from tax_risk.application.business_entertainment.agent import (
    BusinessEntertainmentProfessionalAgent,
)
from tax_risk.application.business_entertainment.evidence_review import (
    build_business_evidence_pack,
)
from tax_risk.domain.semantic.contracts import SemanticLabel, SemanticVersionSet


def _versions() -> SemanticVersionSet:
    return SemanticVersionSet(
        rule_version_id="rule-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version="accounts-v1",
    )


@pytest.mark.parametrize(
    ("reason", "label", "account_id"),
    [
        ("内部培训餐", "EMPLOYEE_EDUCATION", "EMPLOYEE_EDUCATION"),
        ("内部会议餐", "MEETING_EXPENSE", "MEETING_EXPENSE"),
        ("员工聚餐", "EMPLOYEE_WELFARE", "EMPLOYEE_WELFARE"),
        ("接待外部客户商务宴请", "CURRENT_ACCOUNT_REASONABLE", "MANUAL_REVIEW"),
    ],
)
def test_single_item_agent_returns_strict_professional_judgment(
    reason: str,
    label: str,
    account_id: str,
) -> None:
    source_record_id = uuid4()
    pack = build_business_evidence_pack(
        candidate_key="candidate-1",
        snapshot_id=uuid4(),
        canonical_source_record_id=source_record_id,
        fields=(("expense_reason", reason, source_record_id),),
    )
    field = pack.fields[0]
    client = FakeStructuredModelClient(
        [
            {
                "semantic_label": label,
                "confidence_tier": "HIGH",
                "evidence_citations": [
                    {
                        "evidence_id": field.evidence_id,
                        "field_name": field.field_name,
                        "quoted_text": reason,
                    }
                ],
                "recommended_account_ids": [account_id],
                "rationale_summary": "现有证据显示该事项可能属于所述业务场景。",
                "missing_evidence": [],
            }
        ],
        environment="test",
    )
    agent = BusinessEntertainmentProfessionalAgent(client)

    result = asyncio.run(
        agent.evaluate(
            evidence_pack=pack,
            current_account_name="业务招待费",
            document_date=date(2026, 3, 1),
            versions=_versions(),
        )
    )

    assert result.semantic_label is SemanticLabel(label)
    sent = client.calls[0]
    assert sent["input_fields"] == (
        "account_dictionary_version",
        "current_account_name",
        "document_date",
        "evidence",
    )
    assert "chain" not in str(sent).casefold()


def test_agent_never_sends_authority_fields_or_requests_chain_of_thought() -> None:
    source_record_id = uuid4()
    pack = build_business_evidence_pack(
        candidate_key="authority-test",
        snapshot_id=uuid4(),
        canonical_source_record_id=source_record_id,
        fields=(("summary", "会议通知", source_record_id),),
    )
    client = FakeStructuredModelClient(
        [
            {
                "semantic_label": "MEETING_EXPENSE",
                "confidence_tier": "MEDIUM",
                "evidence_citations": [],
                "recommended_account_ids": ["MEETING_EXPENSE"],
                "rationale_summary": "现有证据显示可能属于会议费。",
                "missing_evidence": ["签到材料"],
            }
        ],
        environment="test",
    )
    agent = BusinessEntertainmentProfessionalAgent(client)
    asyncio.run(
        agent.evaluate(
            evidence_pack=pack,
            current_account_name="业务招待费",
            document_date=date(2026, 3, 1),
            versions=_versions(),
        )
    )

    call = client.calls[0]
    serialized = str(call)
    for forbidden in ("company_code", "amount", "snapshot_id", "sap_document_number"):
        assert forbidden not in serialized

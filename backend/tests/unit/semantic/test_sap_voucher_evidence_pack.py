from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tax_risk.application.semantic.evidence_review import (
    CitationResolutionError,
    build_sap_voucher_evidence_pack,
    resolve_citations,
)
from tax_risk.domain.semantic.contracts import (
    ConfidenceTier,
    EvidenceCitation,
    SemanticLabel,
    SemanticModelJudgment,
    SemanticVersionSet,
)
from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)


def _view() -> SnapshotBoundSapExpenseVoucher:
    return SnapshotBoundSapExpenseVoucher(
        company_code="C001",
        fiscal_year=2026,
        period=3,
        posting_date=date(2026, 3, 12),
        document_number="100001",
        line_item="001",
        current_account_code="660203",
        current_account_name="业务招待费",
        amount=Decimal("120.00"),
        currency="CNY",
        summary="内部会议餐及会议通知",
        assignment="OA-123",
        reference="HESI-456",
        reversal_reference=None,
        account_family=AccountFamily.BUSINESS_ENTERTAINMENT,
        projection_id=uuid4(),
        snapshot_id=uuid4(),
        observation_id=uuid4(),
        source_record_id=uuid4(),
    )


def _versions() -> SemanticVersionSet:
    return SemanticVersionSet(
        rule_version_id="rule-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version="accounts-v1",
    )


def _judgment(citation: EvidenceCitation) -> SemanticModelJudgment:
    return SemanticModelJudgment(
        semantic_label=SemanticLabel.MEETING_EXPENSE,
        confidence_tier=ConfidenceTier.HIGH,
        evidence_citations=[citation],
        recommended_account_ids=["MEETING_EXPENSE"],
        rationale_summary="现有证据显示可能应计入会议费。",
        missing_evidence=[],
    )


def test_builds_stable_whitelisted_pack_from_frozen_snapshot_view() -> None:
    view = _view()
    first = build_sap_voucher_evidence_pack(view, _versions())
    second = build_sap_voucher_evidence_pack(view, _versions())

    assert first == second
    assert first.snapshot_id == view.snapshot_id
    assert first.source_record_id == view.source_record_id
    assert {field.field_name for field in first.fields} == {
        "posting_date",
        "current_account_code",
        "current_account_name",
        "summary",
        "assignment",
        "reference",
        "reversal_reference",
    }
    serialized = first.model_dump(mode="json")
    assert "company_code" not in serialized
    assert "amount" not in serialized
    assert "document_number" not in serialized


def test_resolve_citations_rejects_external_wrong_field_and_tampered_quote() -> None:
    pack = build_sap_voucher_evidence_pack(_view(), _versions())
    summary = next(field for field in pack.fields if field.field_name == "summary")

    valid = resolve_citations(
        _judgment(
            EvidenceCitation(
                evidence_id=summary.evidence_id,
                field_name="summary",
                quoted_text="内部会议餐",
            )
        ),
        pack,
    )
    assert valid[0].source_record_id == pack.source_record_id

    invalid = (
        EvidenceCitation(
            evidence_id="external-id",
            field_name="summary",
            quoted_text="内部会议餐",
        ),
        EvidenceCitation(
            evidence_id=summary.evidence_id,
            field_name="assignment",
            quoted_text="OA-123",
        ),
        EvidenceCitation(
            evidence_id=summary.evidence_id,
            field_name="summary",
            quoted_text="客户宴请",
        ),
    )
    for citation in invalid:
        with pytest.raises(CitationResolutionError):
            resolve_citations(_judgment(citation), pack)

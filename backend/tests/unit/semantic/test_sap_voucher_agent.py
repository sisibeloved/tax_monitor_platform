from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from tax_risk.application.donation.policy import DONATION_POLICY
from tax_risk.application.semantic.evidence_review import build_sap_voucher_evidence_pack
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent
from tax_risk.application.welfare.policy import WELFARE_POLICY
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.account_dictionary import (
    AccountEntryStatus,
    SuggestedAccountEntry,
)
from tax_risk.domain.semantic.contracts import SemanticModelJudgment, SemanticVersionSet
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SnapshotBoundSapExpenseVoucher


class FakeStructuredClient:
    def __init__(self) -> None:
        self.label = "CURRENT_ACCOUNT_REASONABLE"
        self.account_ids: list[str] = []
        self.citation_id: str | None = None
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[SemanticModelJudgment],
    ) -> SemanticModelJudgment:
        self.calls.append(input_json)
        evidence = input_json["evidence"]
        assert isinstance(evidence, list)
        summary = next(item for item in evidence if item["field_name"] == "summary")
        evidence_id = self.citation_id or str(summary["evidence_id"])
        return output_model.model_validate(
            {
                "semantic_label": self.label,
                "confidence_tier": "HIGH",
                "evidence_citations": [
                    {
                        "evidence_id": evidence_id,
                        "field_name": "summary",
                        "quoted_text": str(summary["value"]),
                    }
                ],
                "recommended_account_ids": self.account_ids,
                "rationale_summary": "根据SAP凭证摘要形成受约束建议。",
                "missing_evidence": [],
            }
        )


class FakeAccountResolver:
    def __init__(self) -> None:
        self.entries = {
            "BUSINESS_ENTERTAINMENT_EXPENSE": ("660101", "业务招待费"),
            "ADVERTISING_PROMOTION_EXPENSE": ("660201", "广告宣传费"),
        }
        self.calls: list[dict[str, Any]] = []

    def resolve_account(self, **kwargs: Any) -> SuggestedAccountEntry:
        self.calls.append(kwargs)
        account_id = str(kwargs["account_id"])
        if account_id not in self.entries:
            raise ValueError("uncontrolled account")
        code, name = self.entries[account_id]
        return SuggestedAccountEntry(
            account_id=account_id,
            account_code=code,
            account_name=name,
            accounting_classification="PERIOD_EXPENSE",
            allowed_monitor_types=(kwargs["monitor_type"],),
            allowed_labels=(kwargs["semantic_label"],),
            status=AccountEntryStatus.ACTIVE,
        )


def _versions() -> SemanticVersionSet:
    return SemanticVersionSet(
        rule_version_id="rule-v2",
        model_version_id="model-v2",
        prompt_version_id="prompt-v2",
        case_library_version_id="cases-v2",
        account_dictionary_version="candidate-accounts-v2",
    )


def _view(account_family: AccountFamily) -> SnapshotBoundSapExpenseVoucher:
    return SnapshotBoundSapExpenseVoucher(
        company_code="1001",
        fiscal_year=2026,
        period=6,
        posting_date=date(2026, 6, 20),
        document_number="510001",
        line_item="001",
        current_account_code="660205",
        current_account_name="职工福利费",
        amount=Decimal("800.00"),
        currency="CNY",
        summary="客户商务宴请",
        assignment=None,
        reference=None,
        reversal_reference=None,
        account_family=account_family,
        projection_id=uuid4(),
        snapshot_id=uuid4(),
        observation_id=uuid4(),
        source_record_id=uuid4(),
    )


def test_welfare_customer_reception_uses_governed_entertainment_account() -> None:
    client = FakeStructuredClient()
    client.label = "BUSINESS_ENTERTAINMENT"
    client.account_ids = ["BUSINESS_ENTERTAINMENT_EXPENSE"]
    resolver = FakeAccountResolver()
    view = _view(AccountFamily.WELFARE)

    detection = asyncio.run(
        SapVoucherAgent(client, resolver).classify(
            policy=WELFARE_POLICY,
            view=view,
            evidence=build_sap_voucher_evidence_pack(view, _versions()),
            versions=_versions(),
        )
    )

    assert detection.monitoring_type is MonitorType.WELFARE
    assert detection.recommended_account_ids == ["BUSINESS_ENTERTAINMENT_EXPENSE"]
    assert resolver.calls[0]["dictionary_version"] == "candidate-accounts-v2"
    assert "company_code" not in client.calls[0]
    assert "amount" not in client.calls[0]


def test_donation_brand_exposure_uses_governed_advertising_account() -> None:
    client = FakeStructuredClient()
    client.label = "ADVERTISING_PROMOTION"
    client.account_ids = ["ADVERTISING_PROMOTION_EXPENSE"]
    view = _view(AccountFamily.DONATION)

    detection = asyncio.run(
        SapVoucherAgent(client, FakeAccountResolver()).classify(
            policy=DONATION_POLICY,
            view=view,
            evidence=build_sap_voucher_evidence_pack(view, _versions()),
            versions=_versions(),
        )
    )

    assert detection.monitoring_type is MonitorType.DONATION
    assert detection.semantic_label.value == "ADVERTISING_PROMOTION"


def test_mismatched_snapshot_evidence_is_rejected_before_model_call() -> None:
    client = FakeStructuredClient()
    view = _view(AccountFamily.WELFARE)
    other = _view(AccountFamily.WELFARE)

    with pytest.raises(ValueError, match="evidence lineage"):
        asyncio.run(
            SapVoucherAgent(client, FakeAccountResolver()).classify(
                policy=WELFARE_POLICY,
                view=view,
                evidence=build_sap_voucher_evidence_pack(other, _versions()),
                versions=_versions(),
            )
        )

    assert client.calls == []


def test_unknown_citation_or_account_is_rejected() -> None:
    view = _view(AccountFamily.WELFARE)
    evidence = build_sap_voucher_evidence_pack(view, _versions())

    unknown_citation = FakeStructuredClient()
    unknown_citation.label = "BUSINESS_ENTERTAINMENT"
    unknown_citation.account_ids = ["BUSINESS_ENTERTAINMENT_EXPENSE"]
    unknown_citation.citation_id = "not-in-pack"
    with pytest.raises(ValueError):
        asyncio.run(
            SapVoucherAgent(unknown_citation, FakeAccountResolver()).classify(
                policy=WELFARE_POLICY,
                view=view,
                evidence=evidence,
                versions=_versions(),
            )
        )

    unknown_account = FakeStructuredClient()
    unknown_account.label = "BUSINESS_ENTERTAINMENT"
    unknown_account.account_ids = ["UNCONTROLLED_ACCOUNT"]
    with pytest.raises(ValueError, match="uncontrolled account"):
        asyncio.run(
            SapVoucherAgent(unknown_account, FakeAccountResolver()).classify(
                policy=WELFARE_POLICY,
                view=view,
                evidence=evidence,
                versions=_versions(),
            )
        )

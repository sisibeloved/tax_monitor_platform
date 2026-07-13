"""Evidence-constrained classifier shared by SAP-only semantic monitors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from tax_risk.application.semantic.evidence_review import EvidencePack, resolve_citations
from tax_risk.application.semantic.model_client import StructuredModelClient
from tax_risk.application.semantic.prompt_safety import minimize_model_input
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.account_dictionary import SuggestedAccountEntry
from tax_risk.domain.semantic.contracts import (
    SemanticDetection,
    SemanticLabel,
    SemanticModelJudgment,
    SemanticVersionSet,
)
from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)


class SuggestedAccountResolver(Protocol):
    def resolve_account(
        self,
        *,
        dictionary_version: str,
        account_id: str,
        monitor_type: str,
        semantic_label: str,
        effective_on: date,
    ) -> SuggestedAccountEntry: ...


@dataclass(frozen=True, slots=True)
class SapVoucherPolicy:
    monitoring_type: MonitorType
    account_family: AccountFamily
    limit_rate: Decimal
    allowed_labels: frozenset[SemanticLabel]
    suspicious_labels: frozenset[SemanticLabel]
    system_prompt: str


class SapVoucherAgent:
    def __init__(
        self,
        client: StructuredModelClient,
        account_resolver: SuggestedAccountResolver,
    ) -> None:
        self._client = client
        self._account_resolver = account_resolver

    async def classify(
        self,
        *,
        policy: SapVoucherPolicy,
        view: SnapshotBoundSapExpenseVoucher,
        evidence: EvidencePack,
        versions: SemanticVersionSet,
    ) -> SemanticDetection:
        _validate_lineage(policy, view, evidence, versions)
        judgment = await self._client.generate(
            system_prompt=policy.system_prompt,
            input_json=_model_input(evidence, versions),
            output_model=SemanticModelJudgment,
        )
        if judgment.semantic_label not in policy.allowed_labels:
            raise ValueError(f"label not allowed: {judgment.semantic_label.value}")
        if judgment.semantic_label in policy.suspicious_labels:
            if not judgment.recommended_account_ids:
                raise ValueError("a suspicious label requires a controlled account suggestion")
        elif judgment.recommended_account_ids:
            raise ValueError("a non-suspicious label cannot recommend an adjustment account")

        for account_id in judgment.recommended_account_ids:
            self._account_resolver.resolve_account(
                dictionary_version=versions.account_dictionary_version,
                account_id=account_id,
                monitor_type=policy.monitoring_type.value,
                semantic_label=judgment.semantic_label.value,
                effective_on=view.posting_date,
            )
        validated_evidence = resolve_citations(judgment, evidence)
        candidate_key = "|".join(
            (
                policy.monitoring_type.value,
                view.company_code,
                str(view.fiscal_year),
                view.document_number,
                view.line_item,
            )
        )
        return SemanticDetection(
            detection_key=f"{candidate_key}|{versions.model_version_id}",
            candidate_key=candidate_key,
            company_code=view.company_code,
            fiscal_year=view.fiscal_year,
            period=view.period,
            monitoring_type=policy.monitoring_type,
            source_mode="SAP_LINKED",
            canonical_source_record_id=view.source_record_id,
            sap_observation_id=view.observation_id,
            sap_document_number=view.document_number,
            sap_line_item=view.line_item,
            amount=view.amount,
            currency=view.currency,
            snapshot_id=view.snapshot_id,
            exact_evidence_link_id=None,
            versions=versions,
            semantic_label=judgment.semantic_label,
            confidence_tier=judgment.confidence_tier,
            evidence_refs=validated_evidence,
            recommended_account_ids=judgment.recommended_account_ids,
            rationale_summary=judgment.rationale_summary,
            missing_evidence=judgment.missing_evidence,
            detected_at=datetime.now(timezone.utc),
        )


def _validate_lineage(
    policy: SapVoucherPolicy,
    view: SnapshotBoundSapExpenseVoucher,
    evidence: EvidencePack,
    versions: SemanticVersionSet,
) -> None:
    if view.account_family is not policy.account_family:
        raise ValueError("SAP voucher account family does not match monitor policy")
    lineage = (view.source_record_id, view.snapshot_id, view.observation_id)
    evidence_lineage = (
        evidence.source_record_id,
        evidence.snapshot_id,
        evidence.observation_id,
    )
    if lineage != evidence_lineage or evidence.versions != versions:
        raise ValueError("evidence lineage does not match the frozen SAP voucher")


def _model_input(
    evidence: EvidencePack,
    versions: SemanticVersionSet,
) -> dict[str, object]:
    fields: list[dict[str, object]] = []
    for field in evidence.fields:
        minimized = minimize_model_input(
            {"text": field.value},
            allowed_fields=frozenset({"text"}),
        )
        fields.append(
            {
                "evidence_id": field.evidence_id,
                "field_name": field.field_name,
                "value": minimized.get("text", ""),
            }
        )
    return {
        "evidence": fields,
        "account_dictionary_version": versions.account_dictionary_version,
    }


__all__ = ["SapVoucherAgent", "SapVoucherPolicy", "SuggestedAccountResolver"]

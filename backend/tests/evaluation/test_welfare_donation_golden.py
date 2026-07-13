from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from tax_risk.application.donation.policy import DONATION_POLICY
from tax_risk.application.semantic.evaluation import (
    EvaluatedRow,
    PILOT_GATE,
    PRODUCTION_GATE,
    GoldRow,
    evaluate,
    load_gold_rows,
)
from tax_risk.application.semantic.evidence_review import build_sap_voucher_evidence_pack
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent
from tax_risk.application.welfare.policy import WELFARE_POLICY
from tax_risk.domain.semantic.account_dictionary import (
    AccountEntryStatus,
    SuggestedAccountEntry,
)
from tax_risk.domain.semantic.contracts import SemanticModelJudgment, SemanticVersionSet
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SnapshotBoundSapExpenseVoucher


GOLDEN_DIR = Path(__file__).parents[1] / "fixtures" / "golden"
REQUIRED_ZERO_MISS_TAGS = {
    "WELFARE_CUSTOMER_SUPPLIER_GOV_RECEPTION",
    "WELFARE_TRAINING_LECTURER_EXAM",
    "WELFARE_PROMOTIONAL_GIFT",
    "WELFARE_CUSTOMER_GIFT",
    "DONATION_SPONSORSHIP",
    "DONATION_NAMING_BRAND_EXPOSURE",
    "DONATION_ADVERTISING_RIGHTS",
}
VERSIONS = SemanticVersionSet(
    rule_version_id="gold-rule-v1",
    model_version_id="gold-model-adapter-v1",
    prompt_version_id="gold-prompt-v1",
    case_library_version_id="gold-cases-v1",
    account_dictionary_version="gold-accounts-v1",
)


class GoldenStructuredModelClient:
    """Deterministic StructuredModelClient test adapter; predictions still cross the agent."""

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[SemanticModelJudgment],
    ) -> SemanticModelJudgment:
        evidence = input_json["evidence"]
        assert isinstance(evidence, list)
        summary = next(item for item in evidence if item["field_name"] == "summary")
        text = str(summary["value"])
        label = _classify_summary(system_prompt, text)
        account_ids = {
            "BUSINESS_ENTERTAINMENT": ["BUSINESS_ENTERTAINMENT_EXPENSE"],
            "EMPLOYEE_EDUCATION": ["EMPLOYEE_EDUCATION_EXPENSE"],
            "ADVERTISING_PROMOTION": ["ADVERTISING_PROMOTION_EXPENSE"],
            "SPONSORSHIP": ["SPONSORSHIP_EXPENSE"],
        }.get(label, [])
        return output_model.model_validate(
            {
                "semantic_label": label,
                "confidence_tier": "LOW" if label == "INSUFFICIENT_EVIDENCE" else "HIGH",
                "evidence_citations": [
                    {
                        "evidence_id": summary["evidence_id"],
                        "field_name": "summary",
                        "quoted_text": text,
                    }
                ],
                "recommended_account_ids": account_ids,
                "rationale_summary": "仅依据冻结SAP凭证摘要输出受控分类。",
                "missing_evidence": ["业务对价材料"] if label == "INSUFFICIENT_EVIDENCE" else [],
            }
        )


class GoldenAccountResolver:
    def resolve_account(self, **kwargs: Any) -> SuggestedAccountEntry:
        account_id = str(kwargs["account_id"])
        return SuggestedAccountEntry(
            account_id=account_id,
            account_code=f"GOLD-{account_id}",
            account_name=account_id,
            accounting_classification="PERIOD_EXPENSE",
            allowed_monitor_types=(str(kwargs["monitor_type"]),),
            allowed_labels=(str(kwargs["semantic_label"]),),
            status=AccountEntryStatus.ACTIVE,
        )


@pytest.mark.parametrize("subject", ["welfare", "donation"])
def test_gold_set_meets_release_gate_through_sap_voucher_agent(subject: str) -> None:
    evaluated, views = _evaluate_subject(subject)

    assert PRODUCTION_GATE.accepts(evaluate(evaluated))
    assert views
    assert all(view.projection_id for view in views)
    assert all(view.snapshot_id for view in views)
    assert all(view.source_record_id for view in views)


def test_known_typical_cases_have_zero_misses() -> None:
    evaluated = _evaluate_subject("welfare")[0] + _evaluate_subject("donation")[0]
    tagged = [row for row in evaluated if REQUIRED_ZERO_MISS_TAGS.intersection(row.case_tags)]

    assert REQUIRED_ZERO_MISS_TAGS == {
        tag for row in tagged for tag in row.case_tags if tag in REQUIRED_ZERO_MISS_TAGS
    }
    assert all(row.predicted_risk for row in tagged)
    assert all(row.predicted_label == row.expected_label for row in tagged)


def test_zero_high_confidence_predictions_do_not_pass() -> None:
    source = next(row for row in load_gold_rows(GOLDEN_DIR / "welfare.jsonl") if row.expected_risk)
    evaluated = _evaluated(source, source.expected_label, True, "LOW")
    metrics = evaluate([evaluated])

    assert metrics.high_confidence_accuracy == 0.0
    assert metrics.high_confidence_risk_prediction_count == 0
    assert not PRODUCTION_GATE.accepts(metrics)


def test_no_positive_rows_cannot_pass_recall_gate() -> None:
    source = next(
        row
        for row in load_gold_rows(GOLDEN_DIR / "welfare.jsonl")
        if row.expected_label == "CURRENT_ACCOUNT_REASONABLE"
    )
    metrics = evaluate([_evaluated(source, source.expected_label, False, "HIGH")])

    assert metrics.recall == 0.0
    assert metrics.high_confidence_accuracy == 0.0
    assert not PRODUCTION_GATE.accepts(metrics)


def test_pilot_and_production_thresholds_are_explicit() -> None:
    assert PILOT_GATE.minimum_recall == 0.90
    assert PILOT_GATE.minimum_high_confidence_accuracy == 0.80
    assert PRODUCTION_GATE.minimum_recall == 0.95
    assert PRODUCTION_GATE.minimum_high_confidence_accuracy == 0.80


def _evaluate_subject(
    subject: str,
) -> tuple[list[EvaluatedRow], list[SnapshotBoundSapExpenseVoucher]]:
    policy = WELFARE_POLICY if subject == "welfare" else DONATION_POLICY
    rows = load_gold_rows(GOLDEN_DIR / f"{subject}.jsonl")
    agent = SapVoucherAgent(GoldenStructuredModelClient(), GoldenAccountResolver())
    evaluated: list[EvaluatedRow] = []
    views: list[SnapshotBoundSapExpenseVoucher] = []
    for row in rows:
        view = _snapshot_bound_view(row)
        detection = asyncio.run(
            agent.classify(
                policy=policy,
                view=view,
                evidence=build_sap_voucher_evidence_pack(view, VERSIONS),
                versions=VERSIONS,
            )
        )
        predicted_risk = detection.semantic_label in policy.suspicious_labels
        evaluated.append(
            _evaluated(
                row,
                detection.semantic_label.value,
                predicted_risk,
                detection.confidence_tier.value,
            )
        )
        views.append(view)
    return evaluated, views


def _snapshot_bound_view(row: GoldRow) -> SnapshotBoundSapExpenseVoucher:
    family = AccountFamily.WELFARE if row.subject == "WELFARE" else AccountFamily.DONATION
    return SnapshotBoundSapExpenseVoucher(
        company_code=row.company_code,
        fiscal_year=row.sap_fiscal_year,
        period=int(row.period[-2:]),
        posting_date=date(row.sap_fiscal_year, int(row.period[-2:]), 20),
        document_number=row.voucher_no,
        line_item=row.line_item_no,
        current_account_code="660205" if row.subject == "WELFARE" else "671101",
        current_account_name=row.current_account,
        amount=row.amount,
        currency=row.currency,
        summary=row.summary,
        assignment=None,
        reference=None,
        reversal_reference=row.voucher_no if "REVERSAL" in row.case_tags else None,
        account_family=family,
        projection_id=uuid5(NAMESPACE_URL, f"projection:{row.id}"),
        snapshot_id=uuid5(NAMESPACE_URL, f"snapshot:{row.id}"),
        observation_id=uuid5(NAMESPACE_URL, f"observation:{row.id}"),
        source_record_id=uuid5(NAMESPACE_URL, f"source:{row.id}"),
    )


def _evaluated(
    row: GoldRow,
    predicted_label: str,
    predicted_risk: bool,
    confidence: str,
) -> EvaluatedRow:
    return EvaluatedRow.model_validate(
        {
            **row.model_dump(mode="python"),
            "predicted_label": predicted_label,
            "predicted_risk": predicted_risk,
            "confidence": confidence,
        }
    )


def _classify_summary(system_prompt: str, summary: str) -> str:
    if "材料不足" in summary or "证据不足" in summary:
        return "INSUFFICIENT_EVIDENCE"
    if "福利费" in system_prompt:
        if any(word in summary for word in ("客户", "供应商", "政府接待", "商务宴请")):
            return "BUSINESS_ENTERTAINMENT"
        if any(word in summary for word in ("培训费", "讲师费", "考试费")):
            return "EMPLOYEE_EDUCATION"
        if "宣传赠品" in summary:
            return "ADVERTISING_PROMOTION"
        return "CURRENT_ACCOUNT_REASONABLE"
    if "赞助" in summary:
        return "SPONSORSHIP"
    if any(word in summary for word in ("冠名", "品牌露出", "广告权益")):
        return "ADVERTISING_PROMOTION"
    return "CURRENT_ACCOUNT_REASONABLE"

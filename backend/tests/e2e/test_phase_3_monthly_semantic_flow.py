from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from tax_risk.application.donation.policy import DONATION_POLICY
from tax_risk.application.semantic.detection_router import (
    RoutingOutcome,
    RoutingResult,
    decide_detection_route,
)
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent, SapVoucherPolicy
from tax_risk.application.semantic.sap_voucher_monitor import SapVoucherMonitor
from tax_risk.application.welfare.policy import WELFARE_POLICY
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.account_dictionary import (
    AccountEntryStatus,
    SuggestedAccountEntry,
)
from tax_risk.domain.semantic.contracts import (
    SemanticDetection,
    SemanticModelJudgment,
    SemanticVersionSet,
)
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SnapshotBoundSapExpenseVoucher
from tax_risk.persistence.semantic_repositories import ScopeFact


SNAPSHOT_SET_ID = uuid5(NAMESPACE_URL, "phase-3-e2e-snapshot-set")
VERSIONS = SemanticVersionSet(
    rule_version_id="phase-3-rule-v1",
    model_version_id="phase-3-model-v1",
    prompt_version_id="phase-3-prompt-v1",
    case_library_version_id="phase-3-cases-v1",
    account_dictionary_version="phase-3-accounts-v1",
)


class FlowRepository:
    def __init__(self, policy: SapVoucherPolicy) -> None:
        self.policy = policy
        self.snapshot_ids = {
            code: uuid5(NAMESPACE_URL, f"{policy.monitoring_type.value}:{code}")
            for code in ("equal", "above", "missing", "no-lines", "negative")
        }
        rate = policy.limit_rate
        self.facts = {
            "equal": (Decimal("1000") * rate, Decimal("1000")),
            "above": (Decimal("1000") * rate + 1, Decimal("1000")),
            "missing": (Decimal("10"), None),
            "no-lines": (Decimal("1000") * rate + 1, Decimal("1000")),
            "negative": (Decimal("0"), Decimal("-100")),
        }
        self.lines = {
            "above": [
                _line(policy, "above", "001", _risk_summary(policy)),
                _line(policy, "above", "002", _reasonable_summary(policy)),
                _line(policy, "above", "003", "现有证据不足材料不足"),
            ]
        }

    def get_scope_fact(
        self,
        company_code: str,
        period: str,
        monitoring_type: MonitorType,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
    ) -> ScopeFact:
        assert monitoring_type is self.policy.monitoring_type
        assert snapshot_set_id == SNAPSHOT_SET_ID
        assert snapshot_id == self.snapshot_ids[company_code]
        expense, base = self.facts[company_code]
        return ScopeFact(
            company_code=company_code,
            period=period,
            snapshot_set_id=snapshot_set_id,
            snapshot_id=snapshot_id,
            cumulative_expense=expense,
            cumulative_base=base,
        )

    def load_snapshot_bound_sap_vouchers(self, **kwargs: Any):
        return self.lines.get(str(kwargs["company_code"]), [])


class FlowStructuredModelClient:
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
        if "材料不足" in text:
            label, account_ids = "INSUFFICIENT_EVIDENCE", []
        elif "福利费" in system_prompt and "客户" in text:
            label, account_ids = "BUSINESS_ENTERTAINMENT", ["BUSINESS_ENTERTAINMENT"]
        elif "公益性捐赠" in system_prompt and "冠名" in text:
            label, account_ids = "ADVERTISING_PROMOTION", ["ADVERTISING_PROMOTION"]
        else:
            label, account_ids = "CURRENT_ACCOUNT_REASONABLE", []
        return output_model.model_validate(
            {
                "semantic_label": label,
                "confidence_tier": "HIGH",
                "evidence_citations": [
                    {
                        "evidence_id": summary["evidence_id"],
                        "field_name": "summary",
                        "quoted_text": text,
                    }
                ],
                "recommended_account_ids": account_ids,
                "rationale_summary": "端到端受控判断。",
                "missing_evidence": ["业务材料"] if label == "INSUFFICIENT_EVIDENCE" else [],
            }
        )


class FlowAccountResolver:
    def resolve_account(self, **kwargs: Any) -> SuggestedAccountEntry:
        account_id = str(kwargs["account_id"])
        return SuggestedAccountEntry(
            account_id=account_id,
            account_code=f"E2E-{account_id}",
            account_name=account_id,
            accounting_classification="PERIOD_EXPENSE",
            allowed_monitor_types=(str(kwargs["monitor_type"]),),
            allowed_labels=(str(kwargs["semantic_label"]),),
            status=AccountEntryStatus.ACTIVE,
        )


class IdempotentFlowRouter:
    def __init__(self) -> None:
        self.detections: dict[str, UUID] = {}
        self.cases: dict[str, UUID] = {}
        self.evidence_tasks: dict[str, UUID] = {}

    def route(
        self,
        detection: SemanticDetection,
        *,
        suspicious_labels,
    ) -> RoutingResult:
        outcome = decide_detection_route(detection, suspicious_labels)
        detection_created = detection.detection_key not in self.detections
        detection_id = self.detections.setdefault(
            detection.detection_key,
            uuid5(NAMESPACE_URL, f"detection:{detection.detection_key}"),
        )
        evidence_task_id = None
        risk_case_id = None
        case_created = False
        if outcome is RoutingOutcome.EVIDENCE_TASK:
            evidence_task_id = self.evidence_tasks.setdefault(
                detection.detection_key,
                uuid5(NAMESPACE_URL, f"evidence:{detection.detection_key}"),
            )
        elif outcome is RoutingOutcome.RISK_CASE:
            case_created = detection.candidate_key not in self.cases
            risk_case_id = self.cases.setdefault(
                detection.candidate_key,
                uuid5(NAMESPACE_URL, f"case:{detection.candidate_key}"),
            )
        return RoutingResult(
            detection_id,
            outcome,
            evidence_task_id,
            risk_case_id,
            detection_created,
            case_created,
        )


class IssueRecorder:
    def __init__(self) -> None:
        self.items: list[object] = []

    def record(self, issue: object) -> None:
        self.items.append(issue)


@pytest.mark.parametrize("policy", [WELFARE_POLICY, DONATION_POLICY])
def test_scope_to_agent_to_review_routing_flow(policy: SapVoucherPolicy) -> None:
    repository = FlowRepository(policy)
    router = IdempotentFlowRouter()
    issues = IssueRecorder()
    monitor = SapVoucherMonitor(
        policy=policy,
        repository=repository,
        agent=SapVoucherAgent(FlowStructuredModelClient(), FlowAccountResolver()),
        versions=VERSIONS,
        data_issue_recorder=issues,
        router=router,
    )

    results = {
        company: asyncio.run(
            monitor.run(
                company,
                "2026-06",
                SNAPSHOT_SET_ID,
                repository.snapshot_ids[company],
            )
        )
        for company in ("equal", "above", "missing", "no-lines")
    }

    assert results["equal"].selected is False
    assert results["above"].selected is True
    assert results["above"].processed_lines == 3
    assert results["above"].created_or_updated_cases == 1
    assert results["above"].evidence_task_count == 1
    assert results["missing"].status == "NOT_RUN"
    assert results["missing"].issue_code == "MONTHLY_SCOPE_INPUT_MISSING"
    assert results["no-lines"].selected is True
    assert results["no-lines"].processed_lines == 0
    assert len(router.detections) == 3
    assert len(router.cases) == 1
    assert len(router.evidence_tasks) == 1

    if policy is DONATION_POLICY:
        negative = asyncio.run(
            monitor.run(
                "negative",
                "2026-06",
                SNAPSHOT_SET_ID,
                repository.snapshot_ids["negative"],
            )
        )
        assert negative.selected is True
        assert negative.adjustment == "12.00"

    rerun = asyncio.run(
        monitor.run(
            "above",
            "2026-06",
            SNAPSHOT_SET_ID,
            repository.snapshot_ids["above"],
        )
    )
    assert rerun.detection_ids == results["above"].detection_ids
    assert rerun.case_ids == results["above"].case_ids
    assert len(router.detections) == 3
    assert len(router.cases) == 1
    assert len(router.evidence_tasks) == 1


def _line(
    policy: SapVoucherPolicy,
    company_code: str,
    line_item: str,
    summary: str,
) -> SnapshotBoundSapExpenseVoucher:
    snapshot_id = uuid5(NAMESPACE_URL, f"{policy.monitoring_type.value}:{company_code}")
    key = f"{policy.monitoring_type.value}:{company_code}:{line_item}"
    return SnapshotBoundSapExpenseVoucher(
        company_code=company_code,
        fiscal_year=2026,
        period=6,
        posting_date=date(2026, 6, 20),
        document_number=f"{policy.monitoring_type.value[:1]}-{line_item}",
        line_item=line_item,
        current_account_code="660205"
        if policy.account_family is AccountFamily.WELFARE
        else "671101",
        current_account_name="职工福利费"
        if policy.account_family is AccountFamily.WELFARE
        else "公益性捐赠",
        amount=Decimal("100.00"),
        currency="CNY",
        summary=summary,
        account_family=policy.account_family,
        projection_id=uuid5(NAMESPACE_URL, f"projection:{key}"),
        snapshot_id=snapshot_id,
        observation_id=uuid5(NAMESPACE_URL, f"observation:{key}"),
        source_record_id=uuid5(NAMESPACE_URL, f"source:{key}"),
    )


def _risk_summary(policy: SapVoucherPolicy) -> str:
    return "客户商务宴请" if policy is WELFARE_POLICY else "公益活动冠名及品牌露出"


def _reasonable_summary(policy: SapVoucherPolicy) -> str:
    return "员工年度体检" if policy is WELFARE_POLICY else "无对价公益捐赠且材料完整"

"""Shared orchestration for snapshot-bound SAP semantic monitoring."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from tax_risk.application.semantic.detection_router import (
    RoutingOutcome,
    RoutingResult,
)
from tax_risk.application.semantic.evidence_review import build_sap_voucher_evidence_pack
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent, SapVoucherPolicy
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import (
    SemanticDetection,
    SemanticLabel,
    SemanticVersionSet,
)
from tax_risk.domain.semantic.limited_scope import (
    DuplicateScopeMetric,
    MissingScopeInput,
    ScopeInput,
    evaluate_scope,
)
from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)
from tax_risk.persistence.semantic_repositories import ScopeFact


@dataclass(frozen=True, slots=True)
class MonthlyDataIssue:
    company_code: str
    period: str
    monitoring_type: MonitorType
    snapshot_set_id: UUID
    snapshot_id: UUID
    code: str
    details: str


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    status: str
    selected: bool
    adjustment: str | None
    processed_lines: int
    created_or_updated_cases: int
    evidence_task_count: int
    issue_code: str | None = None
    detection_ids: tuple[UUID, ...] = ()
    case_ids: tuple[UUID, ...] = ()


class MonthlySemanticSource(Protocol):
    def get_scope_fact(
        self,
        company_code: str,
        period: str,
        monitoring_type: MonitorType,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
    ) -> ScopeFact: ...

    def load_snapshot_bound_sap_vouchers(
        self,
        *,
        snapshot_set_id: UUID,
        account_family: AccountFamily,
        company_code: str,
        period_end: date,
    ) -> Sequence[SnapshotBoundSapExpenseVoucher]: ...


class DataIssueRecorder(Protocol):
    def record(self, issue: MonthlyDataIssue) -> None: ...


class DetectionRouter(Protocol):
    def route(
        self,
        detection: SemanticDetection,
        *,
        suspicious_labels: frozenset[SemanticLabel],
    ) -> RoutingResult: ...


class SapVoucherMonitor:
    def __init__(
        self,
        *,
        policy: SapVoucherPolicy,
        repository: MonthlySemanticSource,
        agent: SapVoucherAgent,
        versions: SemanticVersionSet,
        data_issue_recorder: DataIssueRecorder,
        router: DetectionRouter,
    ) -> None:
        self._policy = policy
        self._repository = repository
        self._agent = agent
        self._versions = versions
        self._data_issue_recorder = data_issue_recorder
        self._router = router

    async def run(
        self,
        company_code: str,
        period: str,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
    ) -> MonitorRunResult:
        try:
            fact = self._repository.get_scope_fact(
                company_code,
                period,
                self._policy.monitoring_type,
                snapshot_set_id,
                snapshot_id,
            )
            decision = evaluate_scope(
                ScopeInput(
                    company_code=company_code,
                    period=period,
                    cumulative_expense=fact.cumulative_expense,
                    cumulative_base=fact.cumulative_base,
                    limit_rate=self._policy.limit_rate,
                )
            )
        except (MissingScopeInput, DuplicateScopeMetric) as error:
            issue_code = (
                "MONTHLY_SCOPE_METRIC_DUPLICATE"
                if isinstance(error, DuplicateScopeMetric)
                else "MONTHLY_SCOPE_INPUT_MISSING"
            )
            self._record_issue(
                company_code,
                period,
                snapshot_set_id,
                snapshot_id,
                issue_code,
                str(error),
            )
            return MonitorRunResult(
                "NOT_RUN", False, None, 0, 0, 0, issue_code
            )

        adjustment = str(decision.adjustment)
        if not decision.selected:
            return MonitorRunResult("COMPLETED", False, adjustment, 0, 0, 0)

        period_end = _month_end(period)
        lines = self._repository.load_snapshot_bound_sap_vouchers(
            snapshot_set_id=snapshot_set_id,
            account_family=self._policy.account_family,
            company_code=company_code,
            period_end=period_end,
        )
        if any(
            view.snapshot_id != snapshot_id or view.company_code != company_code
            for view in lines
        ):
            issue_code = "MONTHLY_SNAPSHOT_PROJECTION_MISMATCH"
            self._record_issue(
                company_code,
                period,
                snapshot_set_id,
                snapshot_id,
                issue_code,
                "published projection does not match the frozen member snapshot",
            )
            return MonitorRunResult(
                "NOT_RUN", True, adjustment, 0, 0, 0, issue_code
            )

        case_count = 0
        evidence_task_count = 0
        detection_ids: list[UUID] = []
        case_ids: list[UUID] = []
        for view in lines:
            evidence = build_sap_voucher_evidence_pack(view, self._versions)
            detection = await self._agent.classify(
                policy=self._policy,
                view=view,
                evidence=evidence,
                versions=self._versions,
            )
            routed = self._router.route(
                detection,
                suspicious_labels=self._policy.suspicious_labels,
            )
            case_count += int(routed.outcome is RoutingOutcome.RISK_CASE)
            evidence_task_count += int(routed.outcome is RoutingOutcome.EVIDENCE_TASK)
            detection_ids.append(routed.detection_id)
            if routed.risk_case_id is not None:
                case_ids.append(routed.risk_case_id)
        return MonitorRunResult(
            "COMPLETED",
            True,
            adjustment,
            len(lines),
            case_count,
            evidence_task_count,
            detection_ids=tuple(detection_ids),
            case_ids=tuple(case_ids),
        )

    def _record_issue(
        self,
        company_code: str,
        period: str,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
        code: str,
        details: str,
    ) -> None:
        self._data_issue_recorder.record(
            MonthlyDataIssue(
                company_code=company_code,
                period=period,
                monitoring_type=self._policy.monitoring_type,
                snapshot_set_id=snapshot_set_id,
                snapshot_id=snapshot_id,
                code=code,
                details=details,
            )
        )


def _month_end(period: str) -> date:
    try:
        year_text, month_text = period.split("-", maxsplit=1)
        year, month = int(year_text), int(month_text)
        return date(year, month, monthrange(year, month)[1])
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("period must use YYYY-MM format") from error


__all__ = [
    "DataIssueRecorder",
    "DetectionRouter",
    "MonitorRunResult",
    "MonthlyDataIssue",
    "MonthlySemanticSource",
    "SapVoucherMonitor",
]

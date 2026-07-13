"""Welfare monitor composition without duplicate orchestration logic."""

from tax_risk.application.semantic.sap_voucher_monitor import (
    DataIssueRecorder,
    DetectionRouter,
    MonthlySemanticSource,
    SapVoucherMonitor,
)
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent
from tax_risk.application.welfare.policy import WELFARE_POLICY
from tax_risk.domain.semantic.contracts import SemanticVersionSet


def build_welfare_service(
    *,
    repository: MonthlySemanticSource,
    agent: SapVoucherAgent,
    versions: SemanticVersionSet,
    data_issue_recorder: DataIssueRecorder,
    router: DetectionRouter,
) -> SapVoucherMonitor:
    return SapVoucherMonitor(
        policy=WELFARE_POLICY,
        repository=repository,
        agent=agent,
        versions=versions,
        data_issue_recorder=data_issue_recorder,
        router=router,
    )


__all__ = ["build_welfare_service"]

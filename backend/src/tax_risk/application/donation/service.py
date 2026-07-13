"""Donation monitor composition without duplicate orchestration logic."""

from tax_risk.application.donation.policy import DONATION_POLICY
from tax_risk.application.semantic.sap_voucher_agent import SapVoucherAgent
from tax_risk.application.semantic.sap_voucher_monitor import (
    DataIssueRecorder,
    DetectionRouter,
    MonthlySemanticSource,
    SapVoucherMonitor,
)
from tax_risk.domain.semantic.contracts import SemanticVersionSet


def build_donation_service(
    *,
    repository: MonthlySemanticSource,
    agent: SapVoucherAgent,
    versions: SemanticVersionSet,
    data_issue_recorder: DataIssueRecorder,
    router: DetectionRouter,
) -> SapVoucherMonitor:
    return SapVoucherMonitor(
        policy=DONATION_POLICY,
        repository=repository,
        agent=agent,
        versions=versions,
        data_issue_recorder=data_issue_recorder,
        router=router,
    )


__all__ = ["build_donation_service"]

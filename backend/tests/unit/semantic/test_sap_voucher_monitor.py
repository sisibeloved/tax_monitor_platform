from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tax_risk.application.semantic.detection_router import RoutingOutcome, RoutingResult
from tax_risk.application.semantic.sap_voucher_monitor import SapVoucherMonitor
from tax_risk.application.welfare.policy import WELFARE_POLICY
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import SemanticLabel, SemanticVersionSet
from tax_risk.domain.semantic.limited_scope import DuplicateScopeMetric
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SnapshotBoundSapExpenseVoucher
from tax_risk.persistence.semantic_repositories import ScopeFact


SNAPSHOT_SET_ID = uuid4()
SNAPSHOT_ID = uuid4()
VERSIONS = SemanticVersionSet(
    rule_version_id="rules-v1",
    model_version_id="model-v1",
    prompt_version_id="prompt-v1",
    case_library_version_id="cases-v1",
    account_dictionary_version="candidate-accounts-v2",
)


class FakeRepository:
    def __init__(self) -> None:
        self.expense: Decimal | None = Decimal("140.01")
        self.base: Decimal | None = Decimal("1000.00")
        self.lines: list[SnapshotBoundSapExpenseVoucher] = []
        self.duplicate = False
        self.scope_calls: list[tuple[object, ...]] = []
        self.line_calls: list[dict[str, object]] = []

    def get_scope_fact(
        self,
        company_code: str,
        period: str,
        monitoring_type: MonitorType,
        snapshot_set_id: UUID,
        snapshot_id: UUID,
    ) -> ScopeFact:
        self.scope_calls.append(
            (company_code, period, monitoring_type, snapshot_set_id, snapshot_id)
        )
        if self.duplicate:
            raise DuplicateScopeMetric("duplicate")
        return ScopeFact(
            company_code=company_code,
            period=period,
            snapshot_set_id=snapshot_set_id,
            snapshot_id=snapshot_id,
            cumulative_expense=self.expense,
            cumulative_base=self.base,
        )

    def load_snapshot_bound_sap_vouchers(self, **kwargs):
        self.line_calls.append(kwargs)
        return self.lines


class FakeAgent:
    def __init__(self, label: SemanticLabel = SemanticLabel.BUSINESS_ENTERTAINMENT) -> None:
        self.label = label
        self.calls: list[dict[str, object]] = []

    async def classify(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(semantic_label=self.label)


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def route(self, detection, *, suspicious_labels):
        self.calls.append(detection)
        outcome = (
            RoutingOutcome.RISK_CASE
            if detection.semantic_label in suspicious_labels
            else RoutingOutcome.EVIDENCE_TASK
        )
        return RoutingResult(uuid4(), outcome, None, uuid4(), True, True)


@dataclass(frozen=True)
class RecordedIssue:
    code: str


class FakeIssueRecorder:
    def __init__(self) -> None:
        self.items: list[object] = []

    def record(self, issue) -> None:
        self.items.append(issue)


def _line(*, snapshot_id: UUID = SNAPSHOT_ID, document_number: str = "510001"):
    return SnapshotBoundSapExpenseVoucher(
        company_code="1001",
        fiscal_year=2026,
        period=6,
        posting_date=date(2026, 6, 20),
        document_number=document_number,
        line_item="001",
        current_account_code="660205",
        current_account_name="职工福利费",
        amount=Decimal("10.00"),
        currency="CNY",
        summary="客户商务宴请",
        account_family=AccountFamily.WELFARE,
        projection_id=uuid4(),
        snapshot_id=snapshot_id,
        observation_id=uuid4(),
        source_record_id=uuid4(),
    )


def _monitor(
    repository: FakeRepository,
    agent: FakeAgent,
    router: FakeRouter,
    issues: FakeIssueRecorder,
) -> SapVoucherMonitor:
    return SapVoucherMonitor(
        policy=WELFARE_POLICY,
        repository=repository,
        agent=agent,
        versions=VERSIONS,
        data_issue_recorder=issues,
        router=router,
    )


def test_equal_limit_skips_every_model_call() -> None:
    repository, agent, router, issues = (
        FakeRepository(),
        FakeAgent(),
        FakeRouter(),
        FakeIssueRecorder(),
    )
    repository.expense = Decimal("140.00")

    result = asyncio.run(
        _monitor(repository, agent, router, issues).run(
            "1001", "2026-06", SNAPSHOT_SET_ID, SNAPSHOT_ID
        )
    )

    assert result.selected is False
    assert result.processed_lines == 0
    assert agent.calls == []
    assert repository.scope_calls[0][3:] == (SNAPSHOT_SET_ID, SNAPSHOT_ID)


@pytest.mark.parametrize(
    ("duplicate", "expected_code"),
    [
        (False, "MONTHLY_SCOPE_INPUT_MISSING"),
        (True, "MONTHLY_SCOPE_METRIC_DUPLICATE"),
    ],
)
def test_invalid_scope_records_issue_without_model_call(
    duplicate: bool,
    expected_code: str,
) -> None:
    repository, agent, router, issues = (
        FakeRepository(),
        FakeAgent(),
        FakeRouter(),
        FakeIssueRecorder(),
    )
    repository.base = None
    repository.duplicate = duplicate

    result = asyncio.run(
        _monitor(repository, agent, router, issues).run(
            "1001", "2026-06", SNAPSHOT_SET_ID, SNAPSHOT_ID
        )
    )

    assert result.status == "NOT_RUN"
    assert issues.items[0].code == expected_code
    assert agent.calls == []


def test_projection_must_match_frozen_member_snapshot() -> None:
    repository, agent, router, issues = (
        FakeRepository(),
        FakeAgent(),
        FakeRouter(),
        FakeIssueRecorder(),
    )
    repository.lines = [_line(snapshot_id=uuid4())]

    result = asyncio.run(
        _monitor(repository, agent, router, issues).run(
            "1001", "2026-06", SNAPSHOT_SET_ID, SNAPSHOT_ID
        )
    )

    assert result.status == "NOT_RUN"
    assert issues.items[0].code == "MONTHLY_SNAPSHOT_PROJECTION_MISMATCH"
    assert agent.calls == []


def test_selected_company_classifies_every_sap_line() -> None:
    repository, agent, router, issues = (
        FakeRepository(),
        FakeAgent(),
        FakeRouter(),
        FakeIssueRecorder(),
    )
    repository.lines = [_line(document_number="510001"), _line(document_number="510002")]

    result = asyncio.run(
        _monitor(repository, agent, router, issues).run(
            "1001", "2026-06", SNAPSHOT_SET_ID, SNAPSHOT_ID
        )
    )

    assert result.selected is True
    assert result.processed_lines == 2
    assert result.created_or_updated_cases == 2
    assert len(agent.calls) == 2
    assert repository.line_calls == [
        {
            "snapshot_set_id": SNAPSHOT_SET_ID,
            "account_family": AccountFamily.WELFARE,
            "company_code": "1001",
            "period_end": date(2026, 6, 30),
        }
    ]


def test_insufficient_evidence_routes_to_task_not_risk() -> None:
    repository, agent, router, issues = (
        FakeRepository(),
        FakeAgent(SemanticLabel.INSUFFICIENT_EVIDENCE),
        FakeRouter(),
        FakeIssueRecorder(),
    )
    repository.lines = [_line()]

    result = asyncio.run(
        _monitor(repository, agent, router, issues).run(
            "1001", "2026-06", SNAPSHOT_SET_ID, SNAPSHOT_ID
        )
    )

    assert result.created_or_updated_cases == 0
    assert result.evidence_task_count == 1

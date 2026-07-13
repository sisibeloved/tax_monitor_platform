from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentMonthlyService,
    BusinessEntertainmentRunRequest,
    PublishedCompanyInput,
    build_default_business_entertainment_service,
)
from tax_risk.application.business_entertainment.production_pipeline import (
    DatabaseBusinessEntertainmentPipeline,
)
from tax_risk.domain.semantic.contracts import SemanticDetection


class OrderedPipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve_scope(self, period_end: date) -> tuple[str, ...]:
        self.calls.append("scope")
        return ("C001",)

    def load_company_input(
        self, request: BusinessEntertainmentRunRequest
    ) -> PublishedCompanyInput:
        self.calls.append("snapshot")
        return PublishedCompanyInput(
            snapshot_status="PUBLISHED",
            published_at=datetime(2032, 3, 31, tzinfo=timezone.utc),
            source_record_count=4,
            exact_link_count=1,
            fuzzy_hint_count=0,
            conflict_count=0,
            sap_linked_candidate_keys=("linked",),
            business_unlinked_candidate_keys=("unlinked",),
            standalone_sap_keys=("coverage-only",),
        )

    def link_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
        inputs: PublishedCompanyInput,
        *,
        idempotency_key: str,
    ) -> PublishedCompanyInput:
        self.calls.append("link")
        return inputs

    def persist_coverages(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        linked_candidate_keys: tuple[str, ...],
        standalone_sap_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> int:
        self.calls.append("coverage")
        return 2

    def generate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        evaluation_candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[str, ...]:
        self.calls.append("candidate")
        assert "coverage-only" not in evaluation_candidate_keys
        return evaluation_candidate_keys

    def evaluate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[SemanticDetection, ...]:
        self.calls.append("agent")
        return ()

    def route_detections(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        detections: tuple[SemanticDetection, ...],
        idempotency_key: str,
    ) -> tuple[int, int, int]:
        self.calls.append("router")
        return 2, 1, 1


def test_service_orders_scope_snapshot_link_candidate_agent_and_router() -> None:
    pipeline = OrderedPipeline()
    request = BusinessEntertainmentRunRequest(
        run_id=uuid4(),
        company_code="C001",
        period_end=date(2032, 3, 31),
        snapshot_set_id=uuid4(),
        company_list_version_id="company-list-v1",
        rule_version_id="rule-v1",
        lexicon_version="lexicon-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version_id="accounts-v1",
    )

    result = BusinessEntertainmentMonthlyService(pipeline).run_company(
        request,
        task_id="task-1",
    )

    assert pipeline.calls == [
        "scope",
        "snapshot",
        "link",
        "coverage",
        "candidate",
        "agent",
        "router",
    ]
    assert result["detection_count"] == 2
    assert result["risk_case_count"] == 1


def test_default_worker_service_binds_the_database_pipeline() -> None:
    service = build_default_business_entertainment_service()

    assert isinstance(service._pipeline, DatabaseBusinessEntertainmentPipeline)

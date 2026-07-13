from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentMonthlyService,
    BusinessEntertainmentRunRequest,
    PublishedCompanyInput,
)
from tax_risk.config import Settings
from tax_risk.workers.business_entertainment import (
    RUN_COMPANY_TASK,
    register_business_entertainment_tasks,
)
from tax_risk.workers.celery_app import create_celery_app


class RecordingPipeline:
    def __init__(
        self,
        *,
        companies: tuple[str, ...] = ("C001",),
        status: str = "PUBLISHED",
        published_at: datetime | None = datetime(2026, 3, 31, tzinfo=timezone.utc),
    ) -> None:
        self.companies = companies
        self.status = status
        self.published_at = published_at
        self.calls: list[tuple[str, tuple[str, ...] | str]] = []

    def resolve_scope(self, period_end: date) -> tuple[str, ...]:
        self.calls.append(("scope", period_end.isoformat()))
        return self.companies

    def load_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
    ) -> PublishedCompanyInput:
        self.calls.append(("snapshot", request.company_code))
        return PublishedCompanyInput(
            snapshot_status=self.status,
            published_at=self.published_at,
            source_record_count=9,
            exact_link_count=2,
            fuzzy_hint_count=1,
            conflict_count=1,
            sap_linked_candidate_keys=("linked-1",),
            business_unlinked_candidate_keys=("business-1",),
            standalone_sap_keys=("sap-only-1",),
        )

    def persist_coverages(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        linked_candidate_keys: tuple[str, ...],
        standalone_sap_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> int:
        self.calls.append(("coverage", linked_candidate_keys + standalone_sap_keys))
        return len(linked_candidate_keys) + len(standalone_sap_keys)

    def generate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        evaluation_candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[str, ...]:
        self.calls.append(("candidate", evaluation_candidate_keys))
        return evaluation_candidate_keys

    def evaluate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[int, int, int]:
        self.calls.append(("agent", candidate_keys))
        return len(candidate_keys), 1, 1


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused:unused@localhost/unused",
        redis_url="memory://",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
    )


def _request(*, company_code: str = "C001") -> BusinessEntertainmentRunRequest:
    return BusinessEntertainmentRunRequest(
        run_id=uuid4(),
        company_code=company_code,
        period_end=date(2026, 3, 31),
        snapshot_set_id=uuid4(),
        rule_version_id="business-entertainment-rule-v1",
        lexicon_version="business-entertainment-candidates-v1",
    )


def _run_task(
    pipeline: RecordingPipeline,
    request: BusinessEntertainmentRunRequest,
) -> dict[str, object]:
    app = create_celery_app(_settings())
    register_business_entertainment_tasks(
        app=app,
        service_factory=lambda: BusinessEntertainmentMonthlyService(pipeline),
    )
    result = app.tasks[RUN_COMPANY_TASK].delay(**request.to_task_kwargs()).get()
    assert isinstance(result, dict)
    return result


def test_worker_checks_scope_and_snapshot_then_covers_before_agent() -> None:
    pipeline = RecordingPipeline()
    result = _run_task(pipeline, _request())

    assert result["status"] == "SUCCEEDED"
    assert [name for name, _ in pipeline.calls] == [
        "scope",
        "snapshot",
        "coverage",
        "candidate",
        "agent",
    ]
    assert pipeline.calls[2][1] == ("linked-1", "sap-only-1")
    assert pipeline.calls[3][1] == ("linked-1", "business-1")
    assert pipeline.calls[4][1] == ("linked-1", "business-1")
    assert "sap-only-1" not in pipeline.calls[4][1]
    assert result["sap_coverage_count"] == 2
    assert result["detection_count"] == 2
    assert result["evidence_task_count"] == 1
    assert result["risk_case_count"] == 1


def test_retry_uses_stable_idempotency_key() -> None:
    request = _request()
    first = _run_task(RecordingPipeline(), request)
    second = _run_task(RecordingPipeline(), request)

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["task_id"] != second["task_id"]


def test_company_failures_are_isolated_and_fail_closed() -> None:
    invalid_company = _run_task(RecordingPipeline(), _request(company_code="C999"))
    valid_company = _run_task(RecordingPipeline(), _request(company_code="C001"))

    assert invalid_company["status"] == "FAILED"
    assert invalid_company["error_code"] == "COMPANY_OUT_OF_SCOPE"
    assert valid_company["status"] == "SUCCEEDED"


def test_only_published_snapshot_with_utc_publication_time_can_run() -> None:
    request = _request()
    not_published = _run_task(RecordingPipeline(status="VALIDATED"), request)
    missing_publication = _run_task(RecordingPipeline(published_at=None), request)

    assert not_published["status"] == "FAILED"
    assert not_published["error_code"] == "SNAPSHOT_SET_NOT_PUBLISHED"
    assert missing_publication["status"] == "FAILED"
    assert missing_publication["error_code"] == "SNAPSHOT_SET_PUBLICATION_TIME_MISSING"


def test_task_arguments_are_ids_and_controlled_versions_only() -> None:
    request = _request()
    task_kwargs = request.to_task_kwargs()

    assert set(task_kwargs) == {
        "run_id",
        "company_code",
        "period_end",
        "snapshot_set_id",
        "rule_version_id",
        "lexicon_version",
    }
    UUID(task_kwargs["run_id"])
    UUID(task_kwargs["snapshot_set_id"])

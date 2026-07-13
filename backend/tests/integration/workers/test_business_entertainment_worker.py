from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentMonthlyService,
    BusinessEntertainmentRunError,
    BusinessEntertainmentRunRequest,
    PublishedCompanyInput,
)
from tax_risk.domain.semantic.contracts import SemanticDetection
from tax_risk.config import Settings
from tax_risk.security.context import current_principal
from tax_risk.workers.business_entertainment import (
    RUN_COMPANY_TASK,
    build_business_entertainment_task_kwargs,
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

    def link_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
        inputs: PublishedCompanyInput,
        *,
        idempotency_key: str,
    ) -> PublishedCompanyInput:
        self.calls.append(("link", request.company_code))
        return inputs

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
    ) -> tuple[SemanticDetection, ...]:
        self.calls.append(("agent", candidate_keys))
        return ()

    def route_detections(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        detections: tuple[SemanticDetection, ...],
        idempotency_key: str,
    ) -> tuple[int, int, int]:
        self.calls.append(("router", request.company_code))
        return 2, 1, 1


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
        company_list_version_id="company-list-v1",
        rule_version_id="business-entertainment-rule-v1",
        lexicon_version="business-entertainment-candidates-v1",
        model_version_id="model-v1",
        prompt_version_id="prompt-v1",
        case_library_version_id="cases-v1",
        account_dictionary_version_id="accounts-v1",
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
        "link",
        "coverage",
        "candidate",
        "agent",
        "router",
    ]
    assert pipeline.calls[3][1] == ("linked-1", "sap-only-1")
    assert pipeline.calls[4][1] == ("linked-1", "business-1")
    assert pipeline.calls[5][1] == ("linked-1", "business-1")
    assert "sap-only-1" not in pipeline.calls[5][1]
    assert result["sap_coverage_count"] == 2
    assert result["detection_count"] == 2
    assert result["evidence_task_count"] == 1
    assert result["risk_case_count"] == 1
    assert result["run_type"] == "MONTHLY_SEMANTIC"
    assert result["monitor_type"] == "BUSINESS_ENTERTAINMENT"
    assert result["batch_id"] == str(result["run_id"])
    assert result["company"] == "C001"
    assert result["period"] == "2026-03"
    assert result["company_output_ready_at"] is not None


def test_retry_uses_stable_idempotency_key() -> None:
    request = _request()
    first = _run_task(RecordingPipeline(), request)
    second = _run_task(RecordingPipeline(), request)

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["task_id"] != second["task_id"]


def test_company_failures_are_isolated_and_fail_closed() -> None:
    invalid_company = _run_task(RecordingPipeline(), _request(company_code="C999"))
    valid_company = _run_task(RecordingPipeline(), _request(company_code="C001"))

    assert invalid_company["status"] == "BLOCKED"
    assert invalid_company["error_code"] == "COMPANY_OUT_OF_SCOPE"
    assert invalid_company["company_output_ready_at"] is None
    assert invalid_company["retryable"] is False
    assert valid_company["status"] == "SUCCEEDED"


def test_only_published_snapshot_with_utc_publication_time_can_run() -> None:
    request = _request()
    not_published = _run_task(RecordingPipeline(status="VALIDATED"), request)
    missing_publication = _run_task(RecordingPipeline(published_at=None), request)

    assert not_published["status"] == "BLOCKED"
    assert not_published["error_code"] == "SNAPSHOT_SET_NOT_PUBLISHED"
    assert missing_publication["status"] == "BLOCKED"
    assert missing_publication["error_code"] == "SNAPSHOT_SET_PUBLICATION_TIME_MISSING"


def test_worker_passes_snapshot_and_published_versions() -> None:
    request = _request()
    task_kwargs = request.to_task_kwargs()

    assert set(task_kwargs) == {
        "run_id",
        "company_code",
        "period_end",
        "snapshot_set_id",
        "company_list_version_id",
        "rule_version_id",
        "lexicon_version",
        "model_version_id",
        "prompt_version_id",
        "case_library_version_id",
        "account_dictionary_version_id",
    }
    UUID(task_kwargs["run_id"])
    UUID(task_kwargs["snapshot_set_id"])


def test_signed_worker_payload_binds_the_exact_company_and_period() -> None:
    request = _request()
    company_id = uuid4()
    settings = Settings(
        environment="test",
        redis_url="memory://",
        celery_task_always_eager=True,
        celery_task_eager_propagates=True,
        celery_task_store_eager_result=True,
        worker_scope_secret="signed-entertainment-worker-scope-test",
    )

    class ScopeAwareService:
        def run_company(
            self,
            current: BusinessEntertainmentRunRequest,
            *,
            task_id: str,
        ) -> dict[str, object]:
            principal = current_principal()
            assert principal is not None and principal.is_service
            assert principal.allowed_company_ids == frozenset({company_id})
            assert principal.service_scope is not None
            assert principal.service_scope.period == request.period_end
            return BusinessEntertainmentMonthlyService(RecordingPipeline()).run_company(
                current,
                task_id=task_id,
            )

    app = create_celery_app(settings)
    register_business_entertainment_tasks(
        app=app,
        service_factory=ScopeAwareService,
    )

    result = (
        app.tasks[RUN_COMPANY_TASK]
        .delay(
            **build_business_entertainment_task_kwargs(
                request,
                company_id=company_id,
                worker_scope_secret=settings.worker_scope_secret,
            )
        )
        .get()
    )

    assert result["status"] == "SUCCEEDED"


def test_production_worker_rejects_unsigned_payload_before_service_access() -> None:
    request = _request()
    calls = 0

    class RecordingService:
        def run_company(
            self,
            current: BusinessEntertainmentRunRequest,
            *,
            task_id: str,
        ) -> dict[str, object]:
            nonlocal calls
            del current, task_id
            calls += 1
            return {}

    app = create_celery_app(
        Settings(
            environment="production",
            redis_url="memory://",
            celery_task_always_eager=True,
            celery_task_eager_propagates=True,
            celery_task_store_eager_result=True,
            export_download_secret="test-production-export-secret-32-chars",
            worker_scope_secret="test-production-worker-secret-32-chars",
        )
    )
    register_business_entertainment_tasks(
        app=app,
        service_factory=RecordingService,
    )

    result = app.tasks[RUN_COMPANY_TASK].delay(**request.to_task_kwargs()).get()

    assert calls == 0
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "WORKER_SCOPE_TOKEN_INVALID"
    assert result["company_output_ready_at"] is None
    assert result["run_type"] == "MONTHLY_SEMANTIC"


def test_stable_provider_failure_retries_once_with_bounded_policy_then_succeeds() -> None:
    request = _request()

    class RetryOnceService:
        def __init__(self) -> None:
            self.calls = 0

        def run_company(
            self,
            current: BusinessEntertainmentRunRequest,
            *,
            task_id: str,
        ) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise BusinessEntertainmentRunError(
                    "PROVIDER_TIMEOUT",
                    "temporary provider timeout",
                    retryable=True,
                )
            return BusinessEntertainmentMonthlyService(RecordingPipeline()).run_company(
                current,
                task_id=task_id,
            )

    service = RetryOnceService()
    app = create_celery_app(
        Settings(
            database_url="postgresql+psycopg://unused:unused@localhost/unused",
            redis_url="memory://",
            environment="test",
            celery_task_always_eager=True,
            celery_task_eager_propagates=False,
            celery_task_store_eager_result=True,
            quarterly_task_max_retries=1,
            quarterly_task_retry_backoff_seconds=1,
        )
    )
    register_business_entertainment_tasks(app=app, service_factory=lambda: service)

    result = app.tasks[RUN_COMPANY_TASK].delay(**request.to_task_kwargs()).get()

    assert service.calls == 2
    assert result["status"] == "SUCCEEDED"
    assert result["retry_count"] == 1

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from tax_risk.config import Settings
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    build_quarterly_batch_canvas,
    register_quarterly_tasks,
)


@dataclass
class _RecordingBatchService:
    statuses: dict[UUID, str]
    company_calls: list[tuple[UUID, str]] = field(default_factory=list)
    reconcile_calls: list[tuple[UUID, list[dict[str, object]]]] = field(
        default_factory=list
    )
    summary_calls: list[UUID] = field(default_factory=list)

    def run_company(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
        automatic_retry_pending: bool = False,
    ) -> dict[str, object]:
        del automatic_retry_pending
        self.company_calls.append((run_company_id, task_id))
        return {
            "run_company_id": str(run_company_id),
            "status": self.statuses[run_company_id],
            "detection_ids": [],
            "case_ids": [],
            "error_code": None,
            "retryable": False,
        }

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None:
        self.reconcile_calls.append((run_id, header_results))

    def summarize(self, *, run_id: UUID) -> dict[str, object]:
        self.summary_calls.append(run_id)
        values = tuple(self.statuses.values())
        return {
            "run_id": str(run_id),
            "status": "PARTIAL_SUCCESS",
            "requested_company_count": len(values),
            "succeeded_company_count": values.count("SUCCEEDED"),
            "blocked_company_count": values.count("BLOCKED"),
            "failed_company_count": values.count("FAILED"),
        }


def test_eager_chord_treats_company_outcomes_as_data_and_always_summarizes() -> None:
    run_id = uuid4()
    run_company_ids = (uuid4(), uuid4(), uuid4())
    service = _RecordingBatchService(
        statuses={
            run_company_ids[0]: "SUCCEEDED",
            run_company_ids[1]: "BLOCKED",
            run_company_ids[2]: "FAILED",
        }
    )
    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=0,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=lambda: service)

    result = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=run_company_ids,
    ).apply_async().get(timeout=10)

    assert result == {
        "run_id": str(run_id),
        "status": "PARTIAL_SUCCESS",
        "requested_company_count": 3,
        "succeeded_company_count": 1,
        "blocked_company_count": 1,
        "failed_company_count": 1,
    }
    assert [run_company_id for run_company_id, _ in service.company_calls] == list(
        run_company_ids
    )
    assert all(task_id for _, task_id in service.company_calls)
    assert len({task_id for _, task_id in service.company_calls}) == 3
    assert len(service.reconcile_calls) == 1
    assert service.reconcile_calls[0][0] == run_id
    assert [item["status"] for item in service.reconcile_calls[0][1]] == [
        "SUCCEEDED",
        "BLOCKED",
        "FAILED",
    ]
    assert service.summary_calls == [run_id]


@dataclass
class _BoundaryFailureService:
    reconcile_calls: list[tuple[UUID, list[dict[str, object]]]] = field(
        default_factory=list
    )
    summary_calls: list[UUID] = field(default_factory=list)

    def run_company(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
        automatic_retry_pending: bool = False,
    ) -> dict[str, object]:
        del run_company_id, task_id, automatic_retry_pending
        raise RuntimeError("database boundary unavailable")

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None:
        self.reconcile_calls.append((run_id, header_results))

    def summarize(self, *, run_id: UUID) -> dict[str, object]:
        self.summary_calls.append(run_id)
        return {"run_id": str(run_id), "status": "FAILED"}


def test_exhausted_boundary_failure_returns_emergency_result_and_summarizes() -> None:
    run_id = uuid4()
    run_company_id = uuid4()
    service = _BoundaryFailureService()
    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=0,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=lambda: service)

    result = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=(run_company_id,),
    ).apply_async().get(timeout=10)

    assert result == {"run_id": str(run_id), "status": "FAILED"}
    assert service.summary_calls == [run_id]
    assert len(service.reconcile_calls) == 1
    emergency = service.reconcile_calls[0][1]
    assert len(emergency) == 1
    assert emergency[0]["run_company_id"] == str(run_company_id)
    assert emergency[0]["status"] == "FAILED"
    assert emergency[0]["retryable"] is False
    assert emergency[0]["error_code"] == "CELERY_TASK_EXECUTION_FAILED"
    assert isinstance(emergency[0]["task_id"], str)
    assert emergency[0]["task_id"]
    assert emergency[0]["detection_ids"] == []
    assert emergency[0]["case_ids"] == []


@dataclass
class _RetryThenSuccessService:
    run_company_calls: list[tuple[UUID, str, bool]] = field(default_factory=list)
    reconcile_calls: list[list[dict[str, object]]] = field(default_factory=list)

    def run_company(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
        automatic_retry_pending: bool = False,
    ) -> dict[str, object]:
        self.run_company_calls.append(
            (run_company_id, task_id, automatic_retry_pending)
        )
        if len(self.run_company_calls) == 1:
            return {
                "run_company_id": str(run_company_id),
                "status": "RETRY_PENDING",
                "retryable": True,
                "task_id": task_id,
                "detection_ids": [],
                "case_ids": [],
                "error_code": "UNEXPECTED_COMPANY_FAILURE",
            }
        return {
            "run_company_id": str(run_company_id),
            "status": "SUCCEEDED",
            "retryable": False,
            "task_id": task_id,
            "detection_ids": [str(uuid4())],
            "case_ids": [],
            "error_code": None,
        }

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None:
        del run_id
        self.reconcile_calls.append(header_results)

    def summarize(self, *, run_id: UUID) -> dict[str, object]:
        return {"run_id": str(run_id), "status": "SUCCEEDED"}


def test_retryable_failed_result_retries_with_same_task_id_then_succeeds() -> None:
    run_id = uuid4()
    run_company_id = uuid4()
    service = _RetryThenSuccessService()
    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=1,
        quarterly_task_retry_backoff_seconds=1,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=lambda: service)

    result = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=(run_company_id,),
    ).apply_async().get(timeout=10)

    assert result == {"run_id": str(run_id), "status": "SUCCEEDED"}
    assert len(service.run_company_calls) == 2
    assert [value[0] for value in service.run_company_calls] == [
        run_company_id,
        run_company_id,
    ]
    assert service.run_company_calls[0][1] == service.run_company_calls[1][1]
    assert [value[2] for value in service.run_company_calls] == [True, False]
    assert len(service.reconcile_calls) == 1
    assert service.reconcile_calls[0][0]["status"] == "SUCCEEDED"


@dataclass
class _SummaryRetryService:
    reconcile_attempts: int = 0
    summary_calls: int = 0

    def run_company(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
        automatic_retry_pending: bool = False,
    ) -> dict[str, object]:
        del automatic_retry_pending
        return {
            "run_company_id": str(run_company_id),
            "status": "SUCCEEDED",
            "retryable": False,
            "task_id": task_id,
            "detection_ids": [],
            "case_ids": [],
            "error_code": None,
        }

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None:
        del run_id, header_results
        self.reconcile_attempts += 1
        if self.reconcile_attempts == 1:
            raise RuntimeError("transient summary database failure")

    def summarize(self, *, run_id: UUID) -> dict[str, object]:
        self.summary_calls += 1
        return {"run_id": str(run_id), "status": "SUCCEEDED"}


def test_summary_task_retries_transient_reconciliation_failure() -> None:
    run_id = uuid4()
    service = _SummaryRetryService()
    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=1,
        quarterly_task_retry_backoff_seconds=1,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=lambda: service)

    result = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=(uuid4(),),
    ).apply_async().get(timeout=10)

    assert result == {"run_id": str(run_id), "status": "SUCCEEDED"}
    assert service.reconcile_attempts == 2
    assert service.summary_calls == 1

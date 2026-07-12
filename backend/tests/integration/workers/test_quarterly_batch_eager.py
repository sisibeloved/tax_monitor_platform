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
    summary_calls: list[UUID] = field(default_factory=list)

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]:
        self.company_calls.append((run_company_id, task_id))
        return {
            "run_company_id": str(run_company_id),
            "status": self.statuses[run_company_id],
            "detection_ids": [],
            "case_ids": [],
            "error_code": None,
        }

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
    assert service.summary_calls == [run_id]

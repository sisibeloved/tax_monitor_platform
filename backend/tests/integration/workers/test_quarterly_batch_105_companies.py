from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, text

from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.application.quarterly_runs import QuarterlyRunService
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    build_quarterly_batch_canvas,
    register_quarterly_tasks,
)


@dataclass
class _InjectedFailureState:
    failed_snapshot_ids: set[UUID]


class _InjectedCompanyFailure(RuntimeError):
    pass


class _QuarterlyBatchSeed(Protocol):
    snapshot_set_id: UUID
    rule_version_id: UUID
    inactive_company_id: UUID
    failed_snapshot_id: UUID


class _CompanyRunner:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        failure_state: _InjectedFailureState,
    ) -> None:
        self._delegate = QuarterlyRunService(uow_factory)
        self._failure_state = failure_state

    def execute(self, *, run_id: UUID, snapshot_id: UUID) -> object:
        if snapshot_id in self._failure_state.failed_snapshot_ids:
            raise _InjectedCompanyFailure("injected worker failure")
        return self._delegate.execute(run_id=run_id, snapshot_id=snapshot_id)


def _status_counts(engine: Engine, run_id: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT status, count(*)
                FROM monitoring_run_company
                WHERE run_id = :run_id
                GROUP BY status
                """
            ),
            {"run_id": run_id},
        )
        return {str(status): count for status, count in rows}


def _run_row(engine: Engine, run_id: UUID) -> dict[str, object]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT * FROM monitoring_run WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().one()
        return dict(row)


def _evidence_counts(engine: Engine, run_id: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        detection = connection.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(DISTINCT detection_key) AS unique_keys,
                       count(DISTINCT company_id) AS companies
                FROM detection_record
                WHERE run_id = :run_id
                """
            ),
            {"run_id": run_id},
        ).mappings().one()
        cases = connection.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(DISTINCT fingerprint) AS unique_fingerprints
                FROM risk_case
                WHERE company_id IN (
                    SELECT member.company_id
                    FROM monitoring_run_company AS company_run
                    JOIN snapshot_set_member AS member
                      ON member.id = company_run.snapshot_set_member_id
                    WHERE company_run.run_id = :run_id
                )
                """
            ),
            {"run_id": run_id},
        ).mappings().one()
        return {
            "detections": detection["total"],
            "unique_detection_keys": detection["unique_keys"],
            "detected_companies": detection["companies"],
            "cases": cases["total"],
            "unique_case_fingerprints": cases["unique_fingerprints"],
        }


def test_105_company_batch_isolates_failures_and_retries_failed_only(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    failure_state = _InjectedFailureState({seed.failed_snapshot_id})

    def batch_service_factory() -> QuarterlyBatchService:
        return QuarterlyBatchService(
            uow_factory,
            company_runner_factory=lambda: _CompanyRunner(uow_factory, failure_state),
        )

    service = batch_service_factory()
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )

    assert plan.run_key == (
        f"quarterly:2026:Q2:{seed.snapshot_set_id}:{seed.rule_version_id}"
    )
    assert len(plan.run_company_ids) == 105
    assert len(set(plan.run_company_ids)) == 105

    # If the process dies after the database commit but before broker publish,
    # resubmitting the same immutable batch must return only its still-pending work.
    redispatch = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    assert redispatch.run_id == plan.run_id
    assert set(redispatch.run_company_ids) == set(plan.run_company_ids)

    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=0,
        quarterly_worker_concurrency=4,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=batch_service_factory)

    first_summary = build_quarterly_batch_canvas(
        app=app,
        run_id=plan.run_id,
        run_company_ids=plan.run_company_ids,
    ).apply_async().get(timeout=120)

    assert first_summary["run_id"] == str(plan.run_id)
    assert first_summary["status"] == "PARTIAL_SUCCESS"
    assert first_summary["requested_company_count"] == 105
    assert first_summary["succeeded_company_count"] == 103
    assert first_summary["blocked_company_count"] == 1
    assert first_summary["failed_company_count"] == 1
    assert _status_counts(engine, plan.run_id) == {
        "BLOCKED": 1,
        "FAILED": 1,
        "SUCCEEDED": 103,
    }
    run = _run_row(engine, plan.run_id)
    assert run["status"] == "PARTIAL_SUCCESS"
    assert run["succeeded_company_count"] == 103
    assert run["blocked_company_count"] == 1
    assert run["failed_company_count"] == 1
    assert run["finished_at"] is not None
    first_finished_at = run["finished_at"]
    assert _evidence_counts(engine, plan.run_id) == {
        "detections": 309,
        "unique_detection_keys": 309,
        "detected_companies": 103,
        "cases": 309,
        "unique_case_fingerprints": 309,
    }

    duplicate_summary = service.summarize(run_id=plan.run_id)
    assert duplicate_summary == first_summary
    assert _run_row(engine, plan.run_id)["finished_at"] == first_finished_at

    replay = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    assert replay.run_id == plan.run_id
    assert replay.run_company_ids == ()

    failure_state.failed_snapshot_ids.clear()
    retry_plan = service.retry_failed(run_id=plan.run_id)
    assert retry_plan.run_id == plan.run_id
    assert len(retry_plan.run_company_ids) == 1

    retry_summary = build_quarterly_batch_canvas(
        app=app,
        run_id=retry_plan.run_id,
        run_company_ids=retry_plan.run_company_ids,
    ).apply_async().get(timeout=30)

    assert retry_summary["status"] == "PARTIAL_SUCCESS"
    assert retry_summary["succeeded_company_count"] == 104
    assert retry_summary["blocked_company_count"] == 1
    assert retry_summary["failed_company_count"] == 0
    assert _status_counts(engine, plan.run_id) == {
        "BLOCKED": 1,
        "SUCCEEDED": 104,
    }
    assert _evidence_counts(engine, plan.run_id) == {
        "detections": 312,
        "unique_detection_keys": 312,
        "detected_companies": 104,
        "cases": 312,
        "unique_case_fingerprints": 312,
    }

    with engine.connect() as connection:
        attempts = connection.execute(
            text(
                """
                SELECT company_run.status, company_run.attempt_count
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.run_id = :run_id
                  AND (
                      member.company_id = :inactive_company_id
                      OR member.snapshot_id = :failed_snapshot_id
                  )
                ORDER BY member.company_id
                """
            ),
            {
                "run_id": plan.run_id,
                "inactive_company_id": seed.inactive_company_id,
                "failed_snapshot_id": seed.failed_snapshot_id,
            },
        ).all()
    assert sorted((str(status), count) for status, count in attempts) == [
        ("BLOCKED", 1),
        ("SUCCEEDED", 2),
    ]

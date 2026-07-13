from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from tax_risk.config import Settings
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    QUARTERLY_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_BATCH_TASK,
    build_quarterly_batch_canvas,
    register_quarterly_tasks,
)
from tax_risk.security.service_scope import verify_service_scope_token


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "redis_url": "redis://localhost:6379/15",
        "environment": "test",
        "celery_task_always_eager": True,
        "quarterly_worker_concurrency": 7,
        "quarterly_task_soft_time_limit_seconds": 120,
        "quarterly_task_time_limit_seconds": 150,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_celery_configuration_is_durable_json_only_and_quarterly_routed() -> None:
    app = create_celery_app(_settings())
    register_quarterly_tasks(
        app=app,
        service_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )

    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert tuple(app.conf.accept_content) == ("json",)
    assert app.conf.enable_utc is True
    assert app.conf.timezone == "UTC"
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_soft_time_limit == 120
    assert app.conf.task_time_limit == 150
    assert app.conf.worker_concurrency == 7
    assert app.conf.task_routes[RUN_COMPANY_TASK]["queue"] == QUARTERLY_QUEUE
    assert app.conf.task_routes[SUMMARIZE_BATCH_TASK]["queue"] == QUARTERLY_QUEUE
    summary_task = app.tasks[SUMMARIZE_BATCH_TASK]
    assert summary_task.max_retries == 3
    assert summary_task.autoretry_for == (Exception,)
    assert summary_task.retry_backoff == 5
    assert summary_task.retry_jitter is True


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "quarterly_task_soft_time_limit_seconds": 150,
            "quarterly_task_time_limit_seconds": 150,
        },
        {
            "quarterly_task_time_limit_seconds": 150,
            "celery_visibility_timeout_seconds": 150,
        },
    ],
    ids=("hard-not-after-soft", "visibility-not-after-hard"),
)
def test_celery_configuration_rejects_unsafe_timeout_order(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _settings(**overrides)


def test_canvas_fans_out_105_id_only_company_tasks_then_summarizes() -> None:
    app = create_celery_app(_settings())
    run_id = uuid4()
    run_company_ids = tuple(uuid4() for _ in range(105))

    canvas = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=run_company_ids,
    )

    assert canvas.subtask_type == "chord"
    header = tuple(canvas.tasks)
    assert len(header) == 105
    assert [signature.task for signature in header] == [RUN_COMPANY_TASK] * 105
    assert [tuple(signature.args) for signature in header] == [
        (str(run_company_id),) for run_company_id in run_company_ids
    ]
    assert all(not signature.kwargs for signature in header)
    assert all(signature.options["queue"] == QUARTERLY_QUEUE for signature in header)

    assert canvas.body.task == SUMMARIZE_BATCH_TASK
    assert tuple(canvas.body.args) == (str(run_id),)
    assert not canvas.body.kwargs
    assert canvas.body.options["queue"] == QUARTERLY_QUEUE


def test_signed_retry_canvas_gives_finalizer_the_complete_batch_scope() -> None:
    app = create_celery_app(_settings())
    run_id = uuid4()
    run_company_id = uuid4()
    retry_company_id = uuid4()
    complete_batch_scope = (retry_company_id, uuid4(), uuid4())
    secret = "signed-quarterly-retry-scope-test"

    canvas = build_quarterly_batch_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=(run_company_id,),
        company_ids=(retry_company_id,),
        summary_company_ids=complete_batch_scope,
        scope_period=date(2026, 6, 30),
        worker_scope_secret=secret,
    )

    company_scope = verify_service_scope_token(
        tuple(canvas.tasks)[0].args[2],
        secret=secret,
        expected_queue=QUARTERLY_QUEUE,
        expected_run_type="QUARTERLY",
        expected_batch_id=str(run_company_id),
    )
    summary_scope = verify_service_scope_token(
        canvas.body.args[1],
        secret=secret,
        expected_queue=QUARTERLY_QUEUE,
        expected_run_type="QUARTERLY_SUMMARY",
        expected_batch_id=str(run_id),
    )

    assert company_scope.company_ids == frozenset({retry_company_id})
    assert summary_scope.company_ids == frozenset(complete_batch_scope)

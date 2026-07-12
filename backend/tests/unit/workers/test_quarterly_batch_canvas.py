from __future__ import annotations

from uuid import uuid4

import pytest

from tax_risk.config import Settings
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    QUARTERLY_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_BATCH_TASK,
    build_quarterly_batch_canvas,
)


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

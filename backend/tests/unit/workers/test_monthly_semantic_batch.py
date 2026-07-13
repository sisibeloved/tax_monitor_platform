from datetime import date
from uuid import uuid4

from celery import Celery

from tax_risk.workers.monthly_semantic import (
    MONTHLY_SEMANTIC_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_TASK,
    build_monthly_semantic_canvas,
)
from tax_risk.security.service_scope import verify_service_scope_token


def test_monthly_canvas_contains_only_id_payloads_on_dedicated_queue() -> None:
    app = Celery("monthly-canvas-test", broker="memory://", backend="cache+memory://")
    run_id = uuid4()
    company_ids = (uuid4(), uuid4())

    canvas = build_monthly_semantic_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=company_ids,
    )

    header = tuple(canvas.tasks)
    assert len(header) == 2
    assert {task.task for task in header} == {RUN_COMPANY_TASK}
    assert {task.options["queue"] for task in header} == {MONTHLY_SEMANTIC_QUEUE}
    assert [task.args for task in header] == [(str(value),) for value in company_ids]
    assert canvas.body.task == SUMMARIZE_TASK
    assert canvas.body.args == (str(run_id),)
    assert canvas.body.options["queue"] == MONTHLY_SEMANTIC_QUEUE


def test_monthly_canvas_signs_each_company_and_the_batch_summary() -> None:
    app = Celery("monthly-signed-canvas", broker="memory://", backend="cache+memory://")
    run_id = uuid4()
    run_company_ids = (uuid4(), uuid4())
    company_ids = (uuid4(), uuid4())
    summary_company_ids = (*company_ids, uuid4())
    secret = "signed-monthly-worker-scope-test"

    canvas = build_monthly_semantic_canvas(
        app=app,
        run_id=run_id,
        run_company_ids=run_company_ids,
        company_ids=company_ids,
        summary_company_ids=summary_company_ids,
        scope_period=date(2026, 6, 30),
        worker_scope_secret=secret,
    )

    for index, task in enumerate(tuple(canvas.tasks)):
        assert task.args[:2] == (str(run_company_ids[index]), str(company_ids[index]))
        scope = verify_service_scope_token(
            task.args[2],
            secret=secret,
            expected_queue=MONTHLY_SEMANTIC_QUEUE,
            expected_run_type="MONTHLY_SEMANTIC",
            expected_batch_id=str(run_company_ids[index]),
        )
        assert scope.company_ids == frozenset({company_ids[index]})
    summary_scope = verify_service_scope_token(
        canvas.body.args[1],
        secret=secret,
        expected_queue=MONTHLY_SEMANTIC_QUEUE,
        expected_run_type="MONTHLY_SEMANTIC_SUMMARY",
        expected_batch_id=str(run_id),
    )
    assert summary_scope.company_ids == frozenset(summary_company_ids)

from uuid import uuid4

from celery import Celery

from tax_risk.workers.monthly_semantic import (
    MONTHLY_SEMANTIC_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_TASK,
    build_monthly_semantic_canvas,
)


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

from uuid import UUID

import pytest
from celery import Celery  # type: ignore[import-untyped]

from tax_risk.observability.context import (
    context_from_headers,
    current_context,
    inject_context_headers,
    install_celery_context,
    observability_context,
)
from tax_risk.observability.metrics import MetricRegistry


def test_context_is_propagated_and_cleared_without_leaking_between_requests() -> None:
    run_id = UUID("11111111-1111-1111-1111-111111111111")
    company_id = UUID("22222222-2222-2222-2222-222222222222")

    with observability_context(
        request_id="request-1",
        task_id="task-1",
        run_id=run_id,
        company_id=company_id,
        fiscal_year=2026,
        period="2026-Q2",
    ):
        assert current_context().as_log_fields() == {
            "request_id": "request-1",
            "task_id": "task-1",
            "run_id": str(run_id),
            "company_id": str(company_id),
            "fiscal_year": 2026,
            "period": "2026-Q2",
        }
        headers = inject_context_headers({})

    assert current_context().as_log_fields() == {}
    propagated = context_from_headers(headers)
    assert propagated.request_id == "request-1"
    assert propagated.task_id == "task-1"
    assert propagated.run_id == run_id
    assert propagated.company_id == company_id
    assert propagated.fiscal_year == 2026
    assert propagated.period == "2026-Q2"


@pytest.mark.parametrize("label", ["company_name", "free_text", "error_message"])
def test_metrics_reject_sensitive_or_unbounded_labels(label: str) -> None:
    registry = MetricRegistry()

    with pytest.raises(ValueError, match="forbidden metric label"):
        registry.counter("tax_risk_invalid_total", "invalid", (label,))


def test_metric_series_accepts_only_declared_bounded_labels() -> None:
    registry = MetricRegistry()
    counter = registry.counter(
        "tax_risk_company_task_total",
        "Company task terminal outcomes.",
        ("run_type", "monitor_type", "status", "error_code"),
    )
    counter.inc(
        {
            "run_type": "QUARTERLY",
            "monitor_type": "TAX_BURDEN",
            "status": "SUCCEEDED",
            "error_code": "NONE",
        }
    )

    with pytest.raises(ValueError, match="metric labels must exactly match"):
        counter.inc({"run_type": "QUARTERLY"})

    rendered = registry.render_prometheus()
    assert "company_name" not in rendered
    assert "tax_risk_company_task_total" in rendered


def test_celery_task_receives_request_and_run_context_with_its_own_task_id() -> None:
    app = Celery("observability-context-test", broker="memory://", backend="cache+memory://")
    app.conf.update(task_always_eager=True, task_store_eager_result=True)
    install_celery_context(app)

    @app.task(shared=False)
    def capture_context() -> dict[str, str | int]:
        return current_context().as_log_fields()

    run_id = UUID("33333333-3333-3333-3333-333333333333")
    with observability_context(request_id="request-worker", run_id=run_id):
        result = capture_context.apply_async(
            headers=inject_context_headers({})
        ).get(timeout=5)

    assert result["request_id"] == "request-worker"
    assert result["run_id"] == str(run_id)
    assert isinstance(result["task_id"], str) and result["task_id"]
    assert isinstance(result["trace_id"], str) and result["trace_id"]
    assert current_context().as_log_fields() == {}

from datetime import timedelta
from functools import partial

from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.persistence.test_constraints import (
    _insert_snapshot_set,
    _populate_snapshot_set,
)
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.observability.metrics import build_default_registry
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


def test_metrics_endpoint_exports_required_bounded_operational_series() -> None:
    registry = build_default_registry()
    registry.metric("tax_risk_company_task_total").inc(
        {
            "run_type": "QUARTERLY",
            "monitor_type": "TAX_BURDEN",
            "status": "FAILED",
            "error_code": "PROVIDER_TIMEOUT",
        }
    )
    client = TestClient(
        create_app(
            settings=Settings(environment="test", semantic_model_provider="fake"),
            metrics_registry=registry,
        )
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    for metric_name in (
        "tax_risk_data_source_ready",
        "tax_risk_quality_block_total",
        "tax_risk_company_task_total",
        "tax_risk_formula_duration_seconds",
        "tax_risk_semantic_candidate_total",
        "tax_risk_semantic_detection_total",
        "tax_risk_semantic_error_total",
        "tax_risk_link_coverage_ratio",
        "tax_risk_evidence_backlog",
        "tax_risk_case_age_seconds",
        "tax_risk_export_total",
        "tax_risk_authorization_failure_total",
        "tax_risk_data_ready_timestamp_seconds",
        "tax_risk_output_ready_timestamp_seconds",
    ):
        assert metric_name in body
    assert "company_name" not in body
    assert "free_text" not in body
    assert "PROVIDER_TIMEOUT" in body


def test_request_context_is_returned_and_api_metric_is_recorded() -> None:
    registry = build_default_registry()
    client = TestClient(
        create_app(
            settings=Settings(environment="test", semantic_model_provider="fake"),
            metrics_registry=registry,
        )
    )

    response = client.get("/health/live", headers={"X-Request-ID": "request-fixed"})

    assert response.headers["X-Request-ID"] == "request-fixed"
    rendered = registry.render_prometheus()
    assert 'path="/health/live"' in rendered
    assert 'status="200"' in rendered


def test_operations_summary_materializes_published_snapshot_before_uow_closes(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    with engine.begin() as connection:
        snapshot_set_id = _insert_snapshot_set(
            connection,
            set_key="operations-detached",
        )
        _populate_snapshot_set(
            connection,
            snapshot_set_id,
            member_count=100,
            company_code_prefix="OPS-",
        )
        published_at = connection.execute(
            text(
                """
                UPDATE snapshot_set SET status = 'PUBLISHED'
                WHERE id = :snapshot_set_id
                RETURNING published_at
                """
            ),
            {"snapshot_set_id": snapshot_set_id},
        ).scalar_one()
    principal = Principal(
        subject="operations-observer",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(environment="test", semantic_model_provider="fake"),
        principal_provider=lambda _request: principal,
    )
    try:
        response = TestClient(app).get("/api/v1/operations/summary")
        assert response.status_code == 200, response.text
        assert response.json()["t_plus_2_deadline"] == (
            published_at + timedelta(hours=48)
        ).isoformat()
    finally:
        engine.dispose()

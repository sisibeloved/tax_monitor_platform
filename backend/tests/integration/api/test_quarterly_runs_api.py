from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
import hmac
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tax_risk.application.quarterly_batches import QuarterlyBatchError, QuarterlyBatchPlan
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


DEV_PRINCIPAL_SECRET = "quarterly-api-development-secret"


def _principal_headers(
    *,
    roles: tuple[str, ...],
    allowed_company_ids: tuple[UUID, ...] = (),
) -> dict[str, str]:
    payload = json.dumps(
        {
            "subject": "tax-api-test@example.com",
            "roles": list(roles),
            "allowed_company_ids": [str(value) for value in allowed_company_ids],
            "organization_path": "/GROUP/TAX",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(
        DEV_PRINCIPAL_SECRET.encode(),
        payload.encode(),
        sha256,
    ).hexdigest()
    return {
        "X-Development-Principal": payload,
        "X-Development-Principal-Signature": signature,
    }


@dataclass
class _RecordingBatchService:
    plan: QuarterlyBatchPlan
    calls: list[dict[str, object]] = field(default_factory=list)
    error: QuarterlyBatchError | None = None

    def start_batch(self, **request: object) -> QuarterlyBatchPlan:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.plan


@dataclass
class _RecordingDispatcher:
    calls: list[tuple[UUID, tuple[UUID, ...]]] = field(default_factory=list)

    def __call__(self, *, run_id: UUID, run_company_ids: tuple[UUID, ...]) -> None:
        self.calls.append((run_id, run_company_ids))


@pytest.fixture
def run_api_resources(
    isolated_database_url: str,
) -> Iterator[
    tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ]
]:
    engine, factory = create_session_factory(isolated_database_url)
    with engine.begin() as connection:
        rule_version_id = connection.execute(
            text(
                """
                SELECT id FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1'
                  AND version = 'phase-1-reviewed'
                """
            )
        ).scalar_one()
        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:key, DATE '2031-03-31', 'DRAFT', 100)
                RETURNING id
                """
            ),
            {"key": f"api-run-set-{uuid4().hex}"},
        ).scalar_one()
        run_key = f"quarterly:2031:Q1:{snapshot_set_id}:{rule_version_id}"
        run_id = connection.execute(
            text(
                """
                INSERT INTO monitoring_run (
                    run_key, run_type, snapshot_set_id, rule_version_id, status,
                    fiscal_year, quarter, requested_company_count,
                    succeeded_company_count, failed_company_count,
                    blocked_company_count, started_at
                ) VALUES (
                    :run_key, 'QUARTERLY', :set_id, :rule_id, 'RUNNING',
                    2031, 1, 2, 0, 0, 0, now()
                ) RETURNING id
                """
            ),
            {
                "run_key": run_key,
                "set_id": snapshot_set_id,
                "rule_id": rule_version_id,
            },
        ).scalar_one()
    run_company_ids = (uuid4(), uuid4())
    service = _RecordingBatchService(
        QuarterlyBatchPlan(
            run_id=run_id,
            run_key=run_key,
            run_company_ids=run_company_ids,
        )
    )
    dispatcher = _RecordingDispatcher()
    settings = Settings.model_validate(
        {
            "environment": "development",
            "development_principal_enabled": True,
            "development_principal_secret": DEV_PRINCIPAL_SECRET,
        }
    )
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=settings,
    )
    app.state.quarterly_batch_service_factory = lambda: service
    app.state.quarterly_dispatcher = dispatcher
    try:
        with TestClient(app) as client:
            yield client, engine, snapshot_set_id, rule_version_id, service, dispatcher
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM monitoring_run_company WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                text("DELETE FROM monitoring_run WHERE id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                text("DELETE FROM snapshot_set WHERE id = :set_id"),
                {"set_id": snapshot_set_id},
            )
        engine.dispose()


def test_post_quarterly_run_uses_injected_dispatcher_without_a_real_broker(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, _, snapshot_set_id, rule_version_id, service, dispatcher = run_api_resources

    response = client.post(
        "/api/v1/quarterly-runs",
        headers=_principal_headers(roles=("group-tax",)),
        json={
            "fiscal_year": 2031,
            "quarter": 1,
            "snapshot_set_id": str(snapshot_set_id),
            "rule_version": str(rule_version_id),
        },
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "run_id": str(service.plan.run_id),
        "run_key": service.plan.run_key,
        "status": "RUNNING",
        "dispatched_company_count": 2,
    }
    assert service.calls == [
        {
            "fiscal_year": 2031,
            "quarter": 1,
            "snapshot_set_id": snapshot_set_id,
            "rule_version_id": rule_version_id,
        }
    ]
    assert dispatcher.calls == [(service.plan.run_id, service.plan.run_company_ids)]


def test_post_quarterly_run_returns_persisted_terminal_status_for_idempotent_run(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, engine, snapshot_set_id, rule_version_id, service, dispatcher = (
        run_api_resources
    )
    service.plan = QuarterlyBatchPlan(
        run_id=service.plan.run_id,
        run_key=service.plan.run_key,
        run_company_ids=(),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE monitoring_run
                SET status = 'SUCCEEDED', requested_company_count = 2,
                    succeeded_company_count = 2, finished_at = now()
                WHERE id = :run_id
                """
            ),
            {"run_id": service.plan.run_id},
        )

    response = client.post(
        "/api/v1/quarterly-runs",
        headers=_principal_headers(roles=("group-tax",)),
        json={
            "fiscal_year": 2031,
            "quarter": 1,
            "snapshot_set_id": str(snapshot_set_id),
            "rule_version": str(rule_version_id),
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["dispatched_company_count"] == 0
    assert dispatcher.calls == []


def test_group_tax_can_read_quarterly_run_with_persisted_counts(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, _, snapshot_set_id, rule_version_id, service, _ = run_api_resources

    response = client.get(
        f"/api/v1/quarterly-runs/{service.plan.run_id}",
        headers=_principal_headers(roles=("group-tax",)),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(service.plan.run_id),
        "run_key": service.plan.run_key,
        "status": "RUNNING",
        "fiscal_year": 2031,
        "quarter": 1,
        "snapshot_set_id": str(snapshot_set_id),
        "rule_version_id": str(rule_version_id),
        "requested_company_count": 2,
        "succeeded_company_count": 0,
        "blocked_company_count": 0,
        "failed_company_count": 0,
        "started_at": response.json()["started_at"],
        "finished_at": None,
        "failure_reason": None,
    }
    assert response.json()["started_at"].endswith("Z")


def test_audit_role_is_a_write_deny_even_when_combined_with_group_tax(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, _, snapshot_set_id, rule_version_id, service, dispatcher = run_api_resources

    response = client.post(
        "/api/v1/quarterly-runs",
        headers=_principal_headers(roles=("audit", "group-tax")),
        json={
            "fiscal_year": 2031,
            "quarter": 1,
            "snapshot_set_id": str(snapshot_set_id),
            "rule_version": str(rule_version_id),
        },
    )

    assert response.status_code == 403
    assert service.calls == []
    assert dispatcher.calls == []


def test_actual_quarterly_manifest_error_is_reported_as_a_conflict(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, _, snapshot_set_id, rule_version_id, service, _ = run_api_resources
    service.error = QuarterlyBatchError(
        "QUARTERLY_RULE_MANIFEST_INVALID",
        "rule manifest is not the approved reviewed definition",
    )

    response = client.post(
        "/api/v1/quarterly-runs",
        headers=_principal_headers(roles=("group-tax",)),
        json={
            "fiscal_year": 2031,
            "quarter": 1,
            "snapshot_set_id": str(snapshot_set_id),
            "rule_version": str(rule_version_id),
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUARTERLY_RULE_MANIFEST_INVALID"


def test_development_principal_rejects_a_tampered_signature(
    run_api_resources: tuple[
        TestClient,
        Engine,
        UUID,
        UUID,
        _RecordingBatchService,
        _RecordingDispatcher,
    ],
) -> None:
    client, _, _, _, service, _ = run_api_resources
    headers = _principal_headers(roles=("group-tax",))
    headers["X-Development-Principal-Signature"] = "0" * 64

    response = client.get(
        f"/api/v1/quarterly-runs/{service.plan.run_id}",
        headers=headers,
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "settings",
    [
        Settings(environment="development", development_principal_enabled=False),
        Settings(
            environment="production",
            development_principal_enabled=False,
            semantic_model_endpoint="https://model.internal.example/generate",
            semantic_model_deployment="income-tax-test",
            semantic_model_credential_ref="secret://test-only-reference",
            export_download_secret="test-production-export-secret-32-chars",
            worker_scope_secret="test-production-worker-secret-32-chars",
        ),
    ],
    ids=["development-disabled", "production"],
)
def test_development_principal_headers_are_rejected_unless_development_enabled(
    isolated_database_url: str,
    settings: Settings,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(uow_factory=partial(UnitOfWork, factory), settings=settings)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/quarterly-runs/{uuid4()}",
            headers=_principal_headers(roles=("group-tax",)),
        )
    engine.dispose()

    assert response.status_code == 401

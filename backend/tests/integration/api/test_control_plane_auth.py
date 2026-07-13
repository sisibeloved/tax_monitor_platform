from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha256
import hmac
import json
from functools import partial
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


DEV_SECRET = "control-plane-auth-test-secret"


def _principal_headers(*, roles: tuple[str, ...]) -> dict[str, str]:
    payload = json.dumps(
        {
            "subject": "control-plane-caller@example.com",
            "roles": list(roles),
            "allowed_company_ids": [],
            "organization_path": "/GROUP",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(DEV_SECRET.encode(), payload.encode(), sha256).hexdigest()
    return {
        "X-Development-Principal": payload,
        "X-Development-Principal-Signature": signature,
    }


def _control_plane_requests() -> tuple[tuple[str, str, dict[str, object]], ...]:
    missing_id = str(uuid4())
    return (
        (
            "POST",
            "/api/v1/ingest-batches",
            {
                "json": {
                    "source": "AUTH_TEST",
                    "source_batch_key": f"auth-{uuid4().hex}",
                    "dataset_code": "quarterly_metric",
                    "extraction_time": "2026-07-01T08:00:00Z",
                    "period": "2026-06-30",
                    "mode": "FULL",
                    "schema_version": "1",
                    "currency": "CNY",
                    "amount_scale": 2,
                    "source_primary_key_definition": {
                        "fields": ["source_record_key"]
                    },
                }
            },
        ),
        (
            "POST",
            f"/api/v1/ingest-batches/{missing_id}/files",
            {"files": {"file": ("source.csv", b"header\n", "text/csv")}},
        ),
        (
            "POST",
            "/api/v1/tax-master/import",
            {
                "data": {
                    "uploaded_by": "untrusted-maker@example.com",
                    "currency": "CNY",
                    "amount_scale": "2",
                },
                "files": {
                    "file": (
                        "master.xlsx",
                        b"not-an-xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            },
        ),
        (
            "POST",
            f"/api/v1/tax-master/{missing_id}/approve",
            {"json": {"reviewed_by": "untrusted-reviewer@example.com"}},
        ),
        (
            "POST",
            "/api/v1/snapshots/validate",
            {
                "json": {
                    "company_code": "AUTH-MISSING",
                    "period": "2026-06-30",
                    "source_batch_ids": [],
                    "accepted_partial_batch_ids": [],
                }
            },
        ),
        ("POST", f"/api/v1/snapshots/{missing_id}/publish", {}),
        (
            "POST",
            "/api/v1/snapshot-sets",
            {
                "json": {
                    "set_key": f"auth-{uuid4().hex}",
                    "period": "2026-06-30",
                    "expected_members": [],
                }
            },
        ),
        ("GET", f"/api/v1/ingest-batches/{missing_id}", {}),
        ("GET", "/api/v1/tax-master/AUTH-MISSING?period=2026-Q2", {}),
    )


@pytest.fixture
def control_plane_clients(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, TestClient, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    production_app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(environment="production"),
    )
    development_app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(
            environment="development",
            development_principal_enabled=True,
            development_principal_secret=DEV_SECRET,
        ),
    )
    with (
        TestClient(production_app) as production_client,
        TestClient(development_app) as development_client,
    ):
        yield production_client, development_client, engine
    engine.dispose()


def test_every_control_plane_endpoint_fails_closed_without_production_identity(
    control_plane_clients: tuple[TestClient, TestClient, Engine],
) -> None:
    production_client, _, _ = control_plane_clients

    for method, url, kwargs in _control_plane_requests():
        response = production_client.request(method, url, **kwargs)
        assert response.status_code == 401, (url, response.status_code, response.text)


@pytest.mark.parametrize("roles", [("company-finance",), ("audit",)])
def test_every_control_plane_endpoint_rejects_non_admin_roles(
    control_plane_clients: tuple[TestClient, TestClient, Engine],
    roles: tuple[str, ...],
) -> None:
    _, development_client, _ = control_plane_clients
    headers = _principal_headers(roles=roles)

    for method, url, kwargs in _control_plane_requests():
        response = development_client.request(method, url, headers=headers, **kwargs)
        assert response.status_code == 403, (url, response.status_code, response.text)


def test_health_check_remains_public_in_production(
    control_plane_clients: tuple[TestClient, TestClient, Engine],
) -> None:
    production_client, _, _ = control_plane_clients

    response = production_client.get("/health")

    assert response.status_code == 200

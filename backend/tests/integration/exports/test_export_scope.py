from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from fastapi.testclient import TestClient

from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)
from tax_risk.application.exports import InMemoryExportObjectStore
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_export_freezes_authorized_scope_and_worker_renders_shared_schema(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    company_code, company_id, snapshot_id, source_id, _, _ = _seed_graph(engine)
    SemanticCaseRouter(partial(UnitOfWork, factory)).route(
        _detection(
            company_code=company_code,
            snapshot_id=snapshot_id,
            source_id=source_id,
            label="MEETING_EXPENSE",
            model_version="async-export-v1",
        ),
        suspicious_labels=frozenset({"MEETING_EXPENSE"}),
    )
    principal = Principal(
        subject="company-exporter",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/companies/scoped",
    )
    store = InMemoryExportObjectStore()
    dispatched: list[str] = []
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(environment="test", export_download_secret="test-secret"),
        principal_provider=lambda _request: principal,
        export_dispatcher=lambda job_id: dispatched.append(str(job_id)),
        export_object_store=store,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/exports",
            json={"export_type": "BUSINESS_ENTERTAINMENT", "filters": {}},
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] == "QUEUED"
        assert job["company_ids"] == [str(company_id)]
        assert dispatched == [job["id"]]

        app.state.export_service.render_export(job["id"])
        completed = client.get(f"/api/v1/exports/{job['id']}")
        assert completed.status_code == 200
        payload = completed.json()
        assert payload["status"] == "COMPLETED"
        assert payload["row_count"] == 1
        assert len(payload["checksum"]) == 64
        assert payload["schema_version"] == "business-entertainment-root-cases-v1"
        assert payload["object_key"].startswith("exports/")
        assert datetime.fromisoformat(payload["expires_at"]) > datetime.now(timezone.utc)
    engine.dispose()


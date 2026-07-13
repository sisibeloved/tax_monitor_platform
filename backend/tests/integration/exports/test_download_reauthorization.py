from __future__ import annotations

from functools import partial
from uuid import uuid4

from fastapi.testclient import TestClient

from tax_risk.application.exports import InMemoryExportObjectStore
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_download_url_is_hidden_after_company_permission_is_revoked(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    company_id = uuid4()
    allowed = Principal(
        subject="company-exporter",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/companies/scoped",
    )
    current = {"principal": allowed}
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(environment="test", export_download_secret="test-secret"),
        principal_provider=lambda _request: current["principal"],
        export_dispatcher=lambda _job_id: None,
        export_object_store=InMemoryExportObjectStore(),
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/exports",
            json={
                "export_type": "BUSINESS_ENTERTAINMENT",
                "filters": {"company_ids": [str(company_id)]},
            },
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        app.state.export_service.complete_for_test(job_id, b"xlsx", row_count=0)

        issued = client.post(f"/api/v1/exports/{job_id}/download-url")
        assert issued.status_code == 200
        assert issued.json()["url"].startswith(f"/api/v1/exports/{job_id}/content?")

        current["principal"] = Principal(
            subject="company-exporter",
            roles=frozenset({COMPANY_FINANCE_ROLE}),
            allowed_company_ids=frozenset(),
            organization_path="/companies/revoked",
        )
        denied = client.post(f"/api/v1/exports/{job_id}/download-url")
        assert denied.status_code == 404
        assert "url" not in denied.text
    engine.dispose()


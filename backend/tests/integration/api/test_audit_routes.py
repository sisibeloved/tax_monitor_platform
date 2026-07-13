from __future__ import annotations

from fastapi.testclient import TestClient

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


def _group_principal(_request) -> Principal:
    return Principal(
        subject="group-auditor",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )


def test_sensitive_read_is_recorded_and_queryable_without_recursive_event(
    isolated_database_url,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=lambda: UnitOfWork(factory),
        settings=Settings(environment="test"),
        principal_provider=_group_principal,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/risk-cases?fiscal_year=2026&quarter=1")
        assert response.status_code == 200

        events = client.get("/api/v1/audit-events?page=1&page_size=20")
        assert events.status_code == 200, events.text
        payload = events.json()
        matching = [
            item for item in payload["items"] if item["action"] == "HTTP_RISK_CASE_LIST"
        ]
        assert len(matching) == 1
        assert matching[0]["actor"] == "group-auditor"
        assert matching[0]["row_count"] == 0
        assert len(matching[0]["filters_hash"]) == 64

        again = client.get("/api/v1/audit-events?page=1&page_size=20")
        assert again.json()["total"] == payload["total"]
    engine.dispose()

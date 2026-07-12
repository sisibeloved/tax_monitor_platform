from fastapi.testclient import TestClient

from tax_risk.main import create_app


def test_health_reports_service_ready() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tax-risk"}

from collections.abc import Sequence

from fastapi.testclient import TestClient

from tax_risk.api.routes.health import ReadinessComponent
from tax_risk.config import Settings
from tax_risk.main import create_app


class RecordingProbe:
    def __init__(self, components: Sequence[ReadinessComponent]) -> None:
        self.components = tuple(components)
        self.calls = 0

    def check(self) -> tuple[ReadinessComponent, ...]:
        self.calls += 1
        return self.components


def test_liveness_checks_only_process_responsiveness() -> None:
    probe = RecordingProbe(
        (ReadinessComponent("postgresql", False, "POSTGRES_UNAVAILABLE"),)
    )
    client = TestClient(
        create_app(
            settings=Settings(environment="test", semantic_model_provider="fake"),
            readiness_probe=probe,
        )
    )

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive", "service": "tax-risk"}
    assert probe.calls == 0


def test_readiness_returns_stable_component_codes_and_503() -> None:
    probe = RecordingProbe(
        (
            ReadinessComponent("postgresql", True, "READY"),
            ReadinessComponent("redis", False, "REDIS_UNAVAILABLE"),
            ReadinessComponent("model_gateway", True, "CONFIG_VALID"),
        )
    )
    client = TestClient(
        create_app(
            settings=Settings(environment="test", semantic_model_provider="fake"),
            readiness_probe=probe,
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": [
            {"component": "postgresql", "ready": True, "code": "READY"},
            {"component": "redis", "ready": False, "code": "REDIS_UNAVAILABLE"},
            {"component": "model_gateway", "ready": True, "code": "CONFIG_VALID"},
        ],
    }
    assert probe.calls == 1


def test_model_gateway_readiness_checks_configuration_without_calling_model() -> None:
    class ConfigurationOnlyProbe:
        def check(self) -> tuple[ReadinessComponent, ...]:
            return (ReadinessComponent("model_gateway", True, "CONFIG_VALID"),)

    client = TestClient(
        create_app(
            settings=Settings(environment="test", semantic_model_provider="fake"),
            readiness_probe=ConfigurationOnlyProbe(),
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"

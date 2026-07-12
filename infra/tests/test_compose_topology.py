"""Static contract tests for the production-shaped Compose topology."""

from __future__ import annotations

import json
from hashlib import sha256
import hmac
import os
from pathlib import Path
import subprocess
from typing import Any


INFRA_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = INFRA_DIR / "docker-compose.yml"
ENV_EXAMPLE = INFRA_DIR / "env.example"
EXPECTED_SERVICES = {
    "postgres",
    "redis",
    "migrate",
    "api",
    "worker-quarterly",
    "web",
}
LONG_LIVED_SERVICES = {"postgres", "redis", "api", "worker-quarterly", "web"}


def _compose_config() -> dict[str, Any]:
    environment = os.environ | {
        "POSTGRES_DB": "tax_risk",
        "POSTGRES_USER": "tax_risk",
        "POSTGRES_PASSWORD": "static-test-only",
        "DATABASE_URL": (
            "postgresql+psycopg://tax_risk:static-test-only@postgres:5432/tax_risk"
        ),
        "REDIS_URL": "redis://redis:6379/0",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=INFRA_DIR.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _local_acceptance_config() -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=INFRA_DIR.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_has_only_the_required_service_topology() -> None:
    services = _compose_config()["services"]

    assert set(services) == EXPECTED_SERVICES
    rendered = json.dumps(services).lower()
    for forbidden in ("agent", "llm", "model-server", "ollama", "openai"):
        assert forbidden not in rendered


def test_application_services_use_immutable_images_without_source_mounts() -> None:
    services = _compose_config()["services"]

    backend_images = {
        services[name]["image"] for name in ("migrate", "api", "worker-quarterly")
    }
    assert len(backend_images) == 1
    for name in ("migrate", "api", "worker-quarterly", "web"):
        assert not services[name].get("volumes")
    for service in services.values():
        assert all(mount["type"] != "bind" for mount in service.get("volumes", []))

    assert services["postgres"]["volumes"][0]["target"] == "/var/lib/postgresql/data"
    assert services["redis"]["volumes"][0]["target"] == "/data"


def test_long_lived_services_are_healthy_and_start_in_dependency_order() -> None:
    services = _compose_config()["services"]

    for name in LONG_LIVED_SERVICES:
        assert services[name].get("healthcheck"), name

    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["worker-quarterly"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker-quarterly"]["depends_on"]["redis"]["condition"] == (
        "service_healthy"
    )
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"

    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert "--queues=quarterly" in services["worker-quarterly"]["command"]
    assert all("sleep" not in json.dumps(service.get("command", "")) for service in services.values())


def test_host_ports_are_loopback_only() -> None:
    services = _compose_config()["services"]

    for name in ("postgres", "redis", "api", "web"):
        assert services[name]["ports"]
        assert all(port["host_ip"] == "127.0.0.1" for port in services[name]["ports"])

    assert services["web"]["ports"][0]["target"] == 8080


def test_base_datastores_can_be_configured_without_application_environment() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
            "REDIS_URL",
            "DEVELOPMENT_PRINCIPAL_SECRET",
        }
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--quiet",
            "postgres",
            "redis",
        ],
        cwd=INFRA_DIR.parent,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_local_acceptance_proxy_injects_a_valid_signed_group_tax_principal() -> None:
    production_web_environment = _compose_config()["services"]["web"]["environment"]
    services = _local_acceptance_config()["services"]
    backend_environment = services["api"]["environment"]
    web_environment = services["web"]["environment"]
    escaped_principal = web_environment["DEVELOPMENT_PRINCIPAL"]
    raw_principal = escaped_principal.replace(r'\"', '"')
    signature = web_environment["DEVELOPMENT_PRINCIPAL_SIGNATURE"]

    assert backend_environment["ENVIRONMENT"] == "development"
    assert backend_environment["DEVELOPMENT_PRINCIPAL_ENABLED"] == "true"
    assert json.loads(raw_principal)["roles"] == ["group-tax"]
    expected = hmac.new(
        backend_environment["DEVELOPMENT_PRINCIPAL_SECRET"].encode(),
        raw_principal.encode(),
        sha256,
    ).hexdigest()
    assert signature == expected
    assert production_web_environment["DEVELOPMENT_PRINCIPAL"] == ""
    assert production_web_environment["DEVELOPMENT_PRINCIPAL_SIGNATURE"] == ""
    assert "DEVELOPMENT_PRINCIPAL_SECRET" not in web_environment
    assert web_environment["NGINX_ENVSUBST_FILTER"].startswith(
        "^DEVELOPMENT_PRINCIPAL(_SIGNATURE)?$"
    )
    assert set(services["web"]["build"]["args"]) == {"VITE_API_BASE_URL"}
    assert services["web"]["build"]["args"]["VITE_API_BASE_URL"] == ""
    assert "http://127.0.0.1:8080/healthz" in services["web"]["healthcheck"]["test"]


def test_container_files_enforce_locked_dependencies_and_server_side_auth_headers() -> None:
    backend_dockerfile = (INFRA_DIR.parent / "backend" / "Dockerfile").read_text()
    backend_dockerignore = (INFRA_DIR.parent / "backend" / ".dockerignore").read_text()
    web_dockerfile = (INFRA_DIR.parent / "web" / "Dockerfile").read_text()
    web_dockerignore = (INFRA_DIR.parent / "web" / ".dockerignore").read_text()
    nginx_template = (INFRA_DIR.parent / "web" / "nginx.conf").read_text()

    assert "COPY requirements.lock" in backend_dockerfile
    assert "--constraint requirements.lock ." in backend_dockerfile
    assert ".env\n" in backend_dockerignore
    assert ".env.*" in backend_dockerignore
    assert ".env\n" in web_dockerignore
    assert ".env.*" in web_dockerignore
    assert "nginxinc/nginx-unprivileged:1.27-alpine" in web_dockerfile
    assert "USER 101" in web_dockerfile
    assert "listen 8080" in nginx_template
    assert "client_max_body_size 50m" in nginx_template
    assert "X-Development-Principal" in nginx_template
    assert "X-Development-Principal-Signature" in nginx_template

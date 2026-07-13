"""Public process health, dependency readiness, and scrape endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Request, Response, status
from redis import Redis
from sqlalchemy import func, select, text

from tax_risk.application.exports import ExportObjectStore
from tax_risk.config import Settings
from tax_risk.observability.metrics import MetricRegistry
from tax_risk.persistence.master_models import RuleVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import SemanticVersionSetRecord


router = APIRouter(tags=["health"])


@dataclass(frozen=True, slots=True)
class ReadinessComponent:
    component: str
    ready: bool
    code: str


class ReadinessProbe(Protocol):
    def check(self) -> tuple[ReadinessComponent, ...]: ...


class DefaultReadinessProbe:
    """Check local dependencies and configuration; never invokes the model provider."""

    def __init__(
        self,
        *,
        settings: Settings,
        uow_factory: Callable[[], UnitOfWork],
        object_store: ExportObjectStore,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._object_store = object_store

    def check(self) -> tuple[ReadinessComponent, ...]:
        return (
            self._check_postgresql(),
            self._check_redis(),
            self._check_object_store(),
            self._check_migration(),
            self._check_versions(),
            self._check_model_configuration(),
        )

    def _check_postgresql(self) -> ReadinessComponent:
        try:
            with self._uow_factory() as uow:
                uow.session.execute(text("SELECT 1")).scalar_one()
        except Exception:
            return ReadinessComponent("postgresql", False, "POSTGRES_UNAVAILABLE")
        return ReadinessComponent("postgresql", True, "READY")

    def _check_redis(self) -> ReadinessComponent:
        client: Redis | None = None
        try:
            client = Redis.from_url(
                self._settings.redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            if client.ping() is not True:
                raise RuntimeError("redis ping failed")
        except Exception:
            return ReadinessComponent("redis", False, "REDIS_UNAVAILABLE")
        finally:
            if client is not None:
                client.close()
        return ReadinessComponent("redis", True, "READY")

    def _check_object_store(self) -> ReadinessComponent:
        object_key = f"health/{uuid4().hex}.probe"
        try:
            self._object_store.put(object_key, b"ready")
            if self._object_store.get(object_key) != b"ready":
                raise RuntimeError("object-store readback mismatch")
            self._object_store.delete(object_key)
        except Exception:
            return ReadinessComponent("object_store", False, "OBJECT_STORE_UNAVAILABLE")
        return ReadinessComponent("object_store", True, "READY")

    def _check_migration(self) -> ReadinessComponent:
        try:
            with self._uow_factory() as uow:
                current = uow.session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            if current != self._settings.expected_migration_head:
                return ReadinessComponent("migration", False, "MIGRATION_HEAD_MISMATCH")
        except Exception:
            return ReadinessComponent("migration", False, "MIGRATION_STATE_UNAVAILABLE")
        return ReadinessComponent("migration", True, "READY")

    def _check_versions(self) -> ReadinessComponent:
        try:
            with self._uow_factory() as uow:
                rule_count = uow.session.scalar(
                    select(func.count(RuleVersion.id)).where(
                        RuleVersion.status == VersionStatus.PUBLISHED
                    )
                )
                semantic_count = uow.session.scalar(
                    select(func.count(SemanticVersionSetRecord.id)).where(
                        SemanticVersionSetRecord.status == "PUBLISHED"
                    )
                )
            if not rule_count or not semantic_count:
                return ReadinessComponent("active_versions", False, "ACTIVE_VERSION_MISSING")
        except Exception:
            return ReadinessComponent("active_versions", False, "VERSION_STATE_UNAVAILABLE")
        return ReadinessComponent("active_versions", True, "READY")

    def _check_model_configuration(self) -> ReadinessComponent:
        if self._settings.semantic_model_provider == "fake":
            return ReadinessComponent("model_gateway", True, "CONFIG_VALID")
        configured = bool(
            self._settings.semantic_model_endpoint
            and self._settings.semantic_model_deployment
            and self._settings.semantic_model_credential_ref
            and self._settings.semantic_model_no_public_training
            and self._settings.semantic_model_zero_retention_required
            and self._settings.semantic_model_retention_mode == "zero"
        )
        if not configured:
            return ReadinessComponent("model_gateway", False, "MODEL_CONFIG_INVALID")
        return ReadinessComponent("model_gateway", True, "CONFIG_VALID")


@router.get("/health/live")
def get_liveness() -> dict[str, str]:
    """Report process responsiveness without touching dependencies."""

    return {"status": "alive", "service": "tax-risk"}


@router.get("/health")
def get_legacy_health() -> dict[str, str]:
    """Keep the original public probe contract for existing deployment manifests."""

    return {"status": "ok", "service": "tax-risk"}


@router.get("/health/ready")
def get_readiness(request: Request, response: Response) -> dict[str, object]:
    probe = cast(ReadinessProbe, request.app.state.readiness_probe)
    components = probe.check()
    ready = all(component.ready for component in components)
    registry = cast(MetricRegistry, request.app.state.metrics_registry)
    metric = registry.metric("tax_risk_readiness_component")
    for component in components:
        metric.set(
            {"component": component.component, "code": component.code},
            1.0 if component.ready else 0.0,
        )
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "components": [asdict(component) for component in components],
    }


@router.get("/metrics")
def get_metrics(request: Request) -> Response:
    registry = cast(MetricRegistry, request.app.state.metrics_registry)
    return Response(
        registry.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


__all__ = [
    "DefaultReadinessProbe",
    "ReadinessComponent",
    "ReadinessProbe",
    "router",
]

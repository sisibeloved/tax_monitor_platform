"""按运行环境关闭式绑定业务招待费Agent模型端口。"""

from __future__ import annotations

from collections.abc import Callable

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseModelConfiguration,
    EnterpriseStructuredModelClient,
    ModelCallAuditSink,
    SqlModelCallAuditSink,
)
from tax_risk.adapters.model.fake_structured_client import FakeStructuredModelClient
from tax_risk.application.semantic.model_client import StructuredModelClient
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork


class BusinessEntertainmentDependencyError(RuntimeError):
    pass


def bind_structured_model_client(
    settings: Settings,
    *,
    credential_resolver: Callable[[str], str],
    uow_factory: Callable[[], UnitOfWork] | None = None,
    audit_sink: ModelCallAuditSink | None = None,
) -> StructuredModelClient | None:
    if settings.semantic_model_provider == "fake":
        if settings.environment != "test":
            raise BusinessEntertainmentDependencyError(
                "fake model client is allowed only in an explicit test environment"
            )
        return FakeStructuredModelClient(
            [
                {
                    "semantic_label": "INSUFFICIENT_EVIDENCE",
                    "confidence_tier": "LOW",
                    "evidence_citations": [],
                    "recommended_account_ids": ["MANUAL_REVIEW"],
                    "rationale_summary": "现有证据不足，建议人工复核。",
                    "missing_evidence": ["需补充业务证据"],
                }
            ],
            environment="test",
        )

    required = {
        "semantic_model_endpoint": settings.semantic_model_endpoint,
        "semantic_model_deployment": settings.semantic_model_deployment,
        "semantic_model_credential_ref": settings.semantic_model_credential_ref,
    }
    missing = tuple(name for name, value in required.items() if not value)
    if missing:
        if settings.environment == "production":
            raise BusinessEntertainmentDependencyError(
                "enterprise model configuration is incomplete: " + ", ".join(missing)
            )
        return None
    if settings.semantic_model_zero_retention_required is not True:
        raise BusinessEntertainmentDependencyError(
            "enterprise model zero-retention configuration is incomplete"
        )
    endpoint = settings.semantic_model_endpoint
    deployment = settings.semantic_model_deployment
    credential_ref = settings.semantic_model_credential_ref
    if endpoint is None or deployment is None or credential_ref is None:
        raise BusinessEntertainmentDependencyError(
            "enterprise model configuration is incomplete"
        )
    resolved_audit_sink = audit_sink
    if resolved_audit_sink is None and uow_factory is not None:
        resolved_audit_sink = SqlModelCallAuditSink(uow_factory)
    return EnterpriseStructuredModelClient(
        EnterpriseModelConfiguration(
            endpoint=endpoint,
            deployment=deployment,
            timeout_seconds=settings.semantic_model_timeout_seconds,
            credential_ref=credential_ref,
            zero_retention_required=True,
        ),
        credential_resolver=credential_resolver,
        audit_sink=resolved_audit_sink,
    )


__all__ = [
    "BusinessEntertainmentDependencyError",
    "bind_structured_model_client",
]

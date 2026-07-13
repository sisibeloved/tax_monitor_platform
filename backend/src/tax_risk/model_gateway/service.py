"""Single construction and execution boundary for enterprise model calls."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseModelConfiguration,
    EnterpriseStructuredModelClient,
    ModelCallAuditSink,
    SqlModelCallAuditSink,
)
from tax_risk.application.semantic.evidence_reader import EvidenceProjection
from tax_risk.application.semantic.model_client import (
    ContextualStructuredModelClient,
    ModelCallContext,
    StructuredModelClient,
)
from tax_risk.model_gateway.policy import ModelGatewayPolicy, ProviderPolicy
from tax_risk.observability.metrics import DEFAULT_METRICS
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


T = TypeVar("T", bound=BaseModel)


class TechnicalReviewRequired(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceReferenceReader(Protocol):
    def read_by_reference(
        self, principal: Principal, reference_id: UUID
    ) -> EvidenceProjection: ...


class ProtectedModelGateway:
    def __init__(
        self,
        client: StructuredModelClient,
        policy: ModelGatewayPolicy,
        *,
        evidence_reader: EvidenceReferenceReader | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._evidence_reader = evidence_reader

    def with_context(self, context: ModelCallContext) -> ProtectedModelGateway:
        client = self._client
        if isinstance(client, ContextualStructuredModelClient):
            client = client.with_context(context)
        return ProtectedModelGateway(
            client,
            self._policy,
            evidence_reader=self._evidence_reader,
        )

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T:
        prepared = self._policy.prepare_payload(input_json)
        DEFAULT_METRICS.metric("tax_risk_semantic_candidate_total").inc(
            {"monitor_type": "MODEL_GATEWAY"}
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                raw = await self._client.generate(
                    system_prompt=system_prompt,
                    input_json=prepared,
                    output_model=output_model,
                )
                candidate: object = raw.model_dump() if isinstance(raw, BaseModel) else raw
                validated = output_model.model_validate(candidate)
                DEFAULT_METRICS.metric("tax_risk_semantic_detection_total").inc(
                    {"monitor_type": "MODEL_GATEWAY", "decision": "VALID"}
                )
                return validated
            except (ValidationError, TypeError, ValueError, AttributeError) as exc:
                last_error = exc
            except Exception:
                DEFAULT_METRICS.metric("tax_risk_semantic_error_total").inc(
                    {
                        "monitor_type": "MODEL_GATEWAY",
                        "error_code": "PROVIDER_FAILURE",
                    }
                )
                raise
        DEFAULT_METRICS.metric("tax_risk_semantic_error_total").inc(
            {"monitor_type": "MODEL_GATEWAY", "error_code": "MODEL_SCHEMA_INVALID"}
        )
        raise TechnicalReviewRequired(
            "MODEL_SCHEMA_INVALID",
            "model output failed strict schema validation twice",
        ) from last_error

    async def generate_from_references(
        self,
        *,
        principal: Principal,
        reference_ids: Iterable[UUID],
        system_prompt: str,
        output_model: type[T],
    ) -> T:
        if self._evidence_reader is None:
            raise TechnicalReviewRequired(
                "EVIDENCE_READER_NOT_CONFIGURED",
                "the protected evidence reader is unavailable",
            )
        evidence: list[dict[str, object]] = []
        for reference_id in reference_ids:
            projection = self._evidence_reader.read_by_reference(principal, reference_id)
            for field_name, value in sorted(projection.payload.items()):
                if isinstance(value, str) and value.strip():
                    evidence.append(
                        {
                            "evidence_id": str(reference_id),
                            "field_name": field_name,
                            "value": value,
                        }
                    )
        return await self.generate(
            system_prompt=system_prompt,
            input_json={"evidence": evidence},
            output_model=output_model,
        )


def build_enterprise_gateway(
    *,
    endpoint: str,
    deployment: str,
    timeout_seconds: float,
    credential_ref: str,
    credential_resolver: Callable[[str], str],
    provider_policy: ProviderPolicy,
    audit_sink: ModelCallAuditSink | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProtectedModelGateway:
    policy = ModelGatewayPolicy(provider_policy)
    adapter = EnterpriseStructuredModelClient(
        EnterpriseModelConfiguration(
            endpoint=endpoint,
            deployment=deployment,
            timeout_seconds=timeout_seconds,
            credential_ref=credential_ref,
            zero_retention_required=True,
        ),
        credential_resolver=credential_resolver,
        audit_sink=audit_sink,
        transport=transport,
    )
    return ProtectedModelGateway(adapter, policy)


def protect_client(
    client: StructuredModelClient,
    provider_policy: ProviderPolicy,
) -> ProtectedModelGateway:
    return ProtectedModelGateway(client, ModelGatewayPolicy(provider_policy))


def sql_model_call_audit_sink(
    uow_factory: Callable[[], UnitOfWork],
) -> ModelCallAuditSink:
    return SqlModelCallAuditSink(uow_factory)


__all__ = [
    "EvidenceReferenceReader",
    "ModelCallAuditSink",
    "ProtectedModelGateway",
    "TechnicalReviewRequired",
    "build_enterprise_gateway",
    "protect_client",
    "sql_model_call_audit_sink",
]

"""Enterprise HTTPS structured-output adapter with privacy-safe call audit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from time import monotonic
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import SemanticModelCallAudit


logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class EnterpriseModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    endpoint: str = Field(min_length=1, max_length=2048)
    deployment: str = Field(min_length=1, max_length=256)
    timeout_seconds: float = Field(gt=0, le=300)
    credential_ref: str = Field(min_length=1, max_length=512)
    zero_retention_required: bool

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_enterprise_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("enterprise model endpoint must use HTTPS")
        return value

    @field_validator("zero_retention_required")
    @classmethod
    def retention_is_mandatory(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("zero_retention_required must be true")
        return value


class ModelCallContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    candidate_key: str = "unknown"
    company_code: str = "unknown"
    model_version_id: str = "unknown"
    prompt_version_id: str = "unknown"
    case_library_version_id: str = "unknown"
    operator_id: str = "system"
    run_id: str = "unknown"


class ModelCallAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: UUID = Field(default_factory=uuid4)
    candidate_key: str
    company_code: str
    model_version_id: str
    prompt_version_id: str
    case_library_version_id: str
    request_checksum: str = Field(min_length=64, max_length=64)
    output_checksum: str = Field(min_length=64, max_length=64)
    allowed_fields: tuple[str, ...]
    token_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    schema_status: str
    retry_count: int = Field(ge=0)
    retention_confirmed: bool
    operator_id: str
    run_id: str
    occurred_at: datetime


class ModelCallAuditSink(Protocol):
    def record(self, audit: ModelCallAuditRecord) -> None: ...


class InMemoryModelCallAuditSink:
    def __init__(self) -> None:
        self.records: list[ModelCallAuditRecord] = []

    def record(self, audit: ModelCallAuditRecord) -> None:
        self.records.append(audit)


class SqlModelCallAuditSink:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def record(self, audit: ModelCallAuditRecord) -> None:
        with self._uow_factory() as uow:
            uow.semantic.add_model_call_audit(
                SemanticModelCallAudit(
                    id=audit.call_id,
                    candidate_key=audit.candidate_key,
                    company_code=audit.company_code,
                    model_version_id=audit.model_version_id,
                    prompt_version_id=audit.prompt_version_id,
                    case_library_version_id=audit.case_library_version_id,
                    request_checksum=audit.request_checksum,
                    output_checksum=audit.output_checksum,
                    allowed_fields=list(audit.allowed_fields),
                    token_count=audit.token_count,
                    latency_ms=audit.latency_ms,
                    schema_status=audit.schema_status,
                    retry_count=audit.retry_count,
                    retention_confirmed=audit.retention_confirmed,
                    operator_id=audit.operator_id,
                    run_id=audit.run_id,
                    occurred_at=audit.occurred_at,
                )
            )
            uow.commit()


class EnterpriseStructuredModelClient:
    def __init__(
        self,
        configuration: EnterpriseModelConfiguration,
        *,
        credential_resolver: Callable[[str], str],
        transport: httpx.AsyncBaseTransport | None = None,
        audit_sink: ModelCallAuditSink | None = None,
        context: ModelCallContext | None = None,
    ) -> None:
        self._configuration = configuration
        self._credential_resolver = credential_resolver
        self._transport = transport
        self._audit_sink = audit_sink or InMemoryModelCallAuditSink()
        self._context = context or ModelCallContext()

    def with_context(self, context: ModelCallContext) -> EnterpriseStructuredModelClient:
        """Return an isolated view that shares transport/audit but owns call metadata."""

        return EnterpriseStructuredModelClient(
            self._configuration,
            credential_resolver=self._credential_resolver,
            transport=self._transport,
            audit_sink=self._audit_sink,
            context=context,
        )

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T:
        credential = self._credential_resolver(self._configuration.credential_ref)
        if not credential:
            raise RuntimeError("enterprise model credential reference could not be resolved")
        request_payload: dict[str, object] = {
            "deployment": self._configuration.deployment,
            "system_prompt": system_prompt,
            "input": input_json,
            "response_schema": output_model.model_json_schema(),
            "zero_retention_required": True,
        }
        request_checksum = _checksum(request_payload)
        started = monotonic()
        output_payload: object = {}
        token_count = 0
        retention_confirmed = False
        schema_status = "FAILED"
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._configuration.timeout_seconds,
            ) as client:
                response = await client.post(
                    self._configuration.endpoint,
                    json=request_payload,
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "X-Zero-Retention-Required": "true",
                        "Content-Type": "application/json",
                    },
                )
            response.raise_for_status()
            response_json = response.json()
            if not isinstance(response_json, dict):
                raise RuntimeError("enterprise model returned a non-object response")
            output_payload = response_json.get("output", {})
            usage = response_json.get("usage", {})
            if isinstance(usage, dict):
                raw_tokens = usage.get("total_tokens", 0)
                token_count = raw_tokens if isinstance(raw_tokens, int) else 0
            retention_confirmed = response_json.get("zero_retention_confirmed") is True
            if not retention_confirmed:
                schema_status = "BLOCKED"
                raise RuntimeError("enterprise model did not confirm zero retention")
            result = output_model.model_validate(output_payload)
            schema_status = "VALID"
            return result
        finally:
            latency_ms = max(0, round((monotonic() - started) * 1000))
            audit = ModelCallAuditRecord(
                candidate_key=self._context.candidate_key,
                company_code=self._context.company_code,
                model_version_id=self._context.model_version_id,
                prompt_version_id=self._context.prompt_version_id,
                case_library_version_id=self._context.case_library_version_id,
                request_checksum=request_checksum,
                output_checksum=_checksum(output_payload),
                allowed_fields=tuple(sorted(input_json)),
                token_count=token_count,
                latency_ms=latency_ms,
                schema_status=schema_status,
                retry_count=0,
                retention_confirmed=retention_confirmed,
                operator_id=self._context.operator_id,
                run_id=self._context.run_id,
                occurred_at=datetime.now(timezone.utc),
            )
            self._audit_sink.record(audit)
            logger.info(
                "semantic_model_call_completed call_id=%s schema_status=%s",
                audit.call_id,
                audit.schema_status,
            )


def _checksum(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return sha256(encoded).hexdigest()


__all__ = [
    "EnterpriseModelConfiguration",
    "EnterpriseStructuredModelClient",
    "InMemoryModelCallAuditSink",
    "ModelCallAuditRecord",
    "ModelCallContext",
    "SqlModelCallAuditSink",
]

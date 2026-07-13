"""Immutable, redacted audit ledger application service."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select

from tax_risk.db import apply_principal_context
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import AuditEvent
from tax_risk.security.policies import Action, DEFAULT_POLICY
from tax_risk.security.principal import Principal


_SENSITIVE_KEYS = frozenset(
    {
        "attachment",
        "attachment_ref",
        "attachment_refs",
        "bank",
        "bank_account",
        "email",
        "evidence",
        "free_text",
        "name",
        "phone",
        "reason",
        "rationale",
        "summary",
    }
)


def normalized_filter_hash(filters: Mapping[str, Any]) -> str:
    normalized = json.dumps(
        filters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(normalized.encode("utf-8")).hexdigest()


def redact_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _redact_value(key, item) for key, item in value.items()}


def _redact_value(key: str, value: Any) -> Any:
    normalized_key = key.casefold()
    if normalized_key in _SENSITIVE_KEYS and value is not None:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return {"sha256": sha256(canonical.encode("utf-8")).hexdigest()}
    if isinstance(value, Mapping):
        return redact_summary(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    action: str
    entity_type: str
    entity_id: UUID
    principal: Principal | None
    company_ids: frozenset[UUID]
    result: str
    request_id: str | None = None
    correlation_id: UUID | None = None
    batch_id: UUID | None = None
    query_id: UUID | None = None
    export_job_id: UUID | None = None
    filters_hash: str | None = None
    row_count: int | None = None
    before_summary: Mapping[str, Any] = field(default_factory=dict)
    after_summary: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


class AuditService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def append(self, draft: AuditEventDraft) -> UUID:
        event_id = uuid4()
        with self._uow_factory() as uow:
            if draft.principal is not None:
                apply_principal_context(uow.session, draft.principal)
            uow.risks.add_audit_event(
                AuditEvent(
                    id=event_id,
                    entity_type=draft.entity_type,
                    entity_id=draft.entity_id,
                    action=draft.action,
                    actor=(draft.principal.subject if draft.principal else "anonymous"),
                    actor_roles=(
                        sorted(draft.principal.roles) if draft.principal else []
                    ),
                    company_ids=sorted(str(value) for value in draft.company_ids),
                    correlation_id=draft.correlation_id,
                    request_id=draft.request_id,
                    batch_id=draft.batch_id,
                    query_id=draft.query_id,
                    export_job_id=draft.export_job_id,
                    filters_hash=draft.filters_hash,
                    row_count=draft.row_count,
                    before_summary=redact_summary(draft.before_summary),
                    after_summary=redact_summary(draft.after_summary),
                    result=draft.result,
                    reason_code=draft.reason_code,
                    payload=redact_summary(draft.payload),
                )
            )
            uow.commit()
        return event_id

    def search(
        self,
        principal: Principal,
        *,
        page: int,
        page_size: int,
    ) -> tuple[int, tuple[AuditEvent, ...]]:
        scope = DEFAULT_POLICY.company_scope(principal, Action.READ_AUDIT)
        conditions = []
        if scope is not None:
            if not scope:
                return 0, ()
            conditions.append(
                or_(
                    *(AuditEvent.company_ids.contains([str(company_id)]) for company_id in scope)
                )
            )
        with self._uow_factory() as uow:
            apply_principal_context(uow.session, principal)
            total = uow.session.scalar(
                select(func.count(AuditEvent.id)).where(*conditions)
            )
            rows = tuple(
                uow.session.scalars(
                    select(AuditEvent)
                    .where(*conditions)
                    .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            uow.session.expunge_all()
        return total or 0, rows


__all__ = [
    "AuditEventDraft",
    "AuditService",
    "normalized_filter_hash",
    "redact_summary",
]

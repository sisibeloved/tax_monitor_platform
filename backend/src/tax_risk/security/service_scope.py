"""Signed, task-bound company scopes for asynchronous workers."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import date
from hashlib import sha256
import hmac
import json
from typing import Any
from uuid import UUID

from tax_risk.security.principal import ServiceScope
from tax_risk.security.policies import Action, DEFAULT_POLICY
from tax_risk.security.principal import MONITOR_SERVICE_ROLE, Principal


class ServiceScopeTokenError(ValueError):
    """The task scope is malformed, unsigned, tampered, or task-mismatched."""


def issue_service_scope_token(
    *,
    secret: str,
    queue: str,
    run_type: str,
    batch_id: str,
    company_ids: frozenset[UUID],
    period: date,
) -> str:
    _validate_binding(secret, queue, run_type, batch_id, company_ids)
    payload = {
        "batch_id": batch_id,
        "company_ids": sorted(str(value) for value in company_ids),
        "period": period.isoformat(),
        "queue": queue,
        "run_type": run_type,
        "version": 1,
    }
    encoded = _encode_payload(payload)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_service_scope_token(
    token: str,
    *,
    secret: str,
    expected_queue: str,
    expected_run_type: str,
    expected_batch_id: str,
) -> ServiceScope:
    if not secret or not token or token.count(".") != 1:
        raise ServiceScopeTokenError("service scope token is invalid")
    encoded, supplied_signature = token.split(".", 1)
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ServiceScopeTokenError("service scope signature is invalid")
    try:
        payload = json.loads(_decode_payload(encoded))
        if not isinstance(payload, dict) or set(payload) != {
            "batch_id",
            "company_ids",
            "period",
            "queue",
            "run_type",
            "version",
        }:
            raise ValueError("unexpected service scope fields")
        if payload["version"] != 1:
            raise ValueError("unsupported service scope version")
        company_ids = frozenset(UUID(value) for value in payload["company_ids"])
        period = date.fromisoformat(payload["period"])
        queue = str(payload["queue"])
        run_type = str(payload["run_type"])
        batch_id = str(payload["batch_id"])
        _validate_binding(secret, queue, run_type, batch_id, company_ids)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceScopeTokenError("service scope payload is invalid") from exc
    if (
        queue != expected_queue
        or run_type != expected_run_type
        or batch_id != expected_batch_id
    ):
        raise ServiceScopeTokenError("service scope is bound to another task")
    return ServiceScope(
        queue=queue,
        run_type=run_type,
        batch_id=batch_id,
        company_ids=company_ids,
        period=period,
        signature_verified=True,
    )


def service_principal(scope: ServiceScope) -> Principal:
    principal = Principal(
        subject=f"{scope.queue}-worker",
        roles=frozenset({MONITOR_SERVICE_ROLE}),
        allowed_company_ids=scope.company_ids,
        organization_path=f"/services/{scope.queue}",
        service_scope=scope,
    )
    DEFAULT_POLICY.require(principal, Action.RUN_MONITOR)
    return principal


def _validate_binding(
    secret: str,
    queue: str,
    run_type: str,
    batch_id: str,
    company_ids: frozenset[UUID],
) -> None:
    if (
        not secret
        or not queue.strip()
        or not run_type.strip()
        or not batch_id.strip()
        or not company_ids
    ):
        raise ServiceScopeTokenError("service scope binding is incomplete")


def _encode_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> str:
    padding = "=" * (-len(encoded) % 4)
    return urlsafe_b64decode(encoded + padding).decode("utf-8")


__all__ = [
    "ServiceScopeTokenError",
    "issue_service_scope_token",
    "service_principal",
    "verify_service_scope_token",
]

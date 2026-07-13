"""Authenticated identity passed from an identity provider into API policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from starlette.requests import Request


GROUP_TAX_ROLE = "group-tax"
DIVISION_TAX_ROLE = "division-tax"
COMPANY_FINANCE_ROLE = "company-finance"
DATA_ADMIN_ROLE = "data-admin"
AUDIT_ROLE = "audit"
MONITOR_SERVICE_ROLE = "monitor-service"
API_ROLES = frozenset(
    {
        GROUP_TAX_ROLE,
        DIVISION_TAX_ROLE,
        COMPANY_FINANCE_ROLE,
        DATA_ADMIN_ROLE,
        AUDIT_ROLE,
    }
)
KNOWN_ROLES = API_ROLES | {MONITOR_SERVICE_ROLE}


@dataclass(frozen=True, slots=True)
class ServiceScope:
    """Signed, immutable execution boundary granted to one monitor worker."""

    queue: str
    run_type: str
    batch_id: str
    company_ids: frozenset[UUID]
    period: date
    signature_verified: bool


@dataclass(frozen=True, slots=True)
class Principal:
    """The identity and company scope authorized by the upstream IdP."""

    subject: str
    roles: frozenset[str]
    allowed_company_ids: frozenset[UUID]
    organization_path: str
    service_scope: ServiceScope | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_service(self) -> bool:
        return self.roles == frozenset({MONITOR_SERVICE_ROLE})


class PrincipalProvider(Protocol):
    """Production identity-provider seam used by application composition."""

    def __call__(self, request: Request) -> Principal: ...


__all__ = [
    "AUDIT_ROLE",
    "API_ROLES",
    "COMPANY_FINANCE_ROLE",
    "DATA_ADMIN_ROLE",
    "DIVISION_TAX_ROLE",
    "GROUP_TAX_ROLE",
    "KNOWN_ROLES",
    "MONITOR_SERVICE_ROLE",
    "Principal",
    "PrincipalProvider",
    "ServiceScope",
]

"""Authenticated identity passed from an identity provider into API policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from starlette.requests import Request


GROUP_TAX_ROLE = "group-tax"
COMPANY_FINANCE_ROLE = "company-finance"
AUDIT_ROLE = "audit"
KNOWN_ROLES = frozenset({GROUP_TAX_ROLE, COMPANY_FINANCE_ROLE, AUDIT_ROLE})


@dataclass(frozen=True, slots=True)
class Principal:
    """The identity and company scope authorized by the upstream IdP."""

    subject: str
    roles: frozenset[str]
    allowed_company_ids: frozenset[UUID]
    organization_path: str

    def has_role(self, role: str) -> bool:
        return role in self.roles


class PrincipalProvider(Protocol):
    """Production identity-provider seam used by application composition."""

    def __call__(self, request: Request) -> Principal: ...


__all__ = [
    "AUDIT_ROLE",
    "COMPANY_FINANCE_ROLE",
    "GROUP_TAX_ROLE",
    "KNOWN_ROLES",
    "Principal",
    "PrincipalProvider",
]

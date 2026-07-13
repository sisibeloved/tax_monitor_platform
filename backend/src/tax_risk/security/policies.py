"""Central authorization matrix and server-owned company-scope decisions."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from tax_risk.security.principal import (
    AUDIT_ROLE,
    COMPANY_FINANCE_ROLE,
    DATA_ADMIN_ROLE,
    DIVISION_TAX_ROLE,
    GROUP_TAX_ROLE,
    MONITOR_SERVICE_ROLE,
    Principal,
)


class Action(StrEnum):
    READ_RISK = "READ_RISK"
    PROCESS_COMPANY_RISK = "PROCESS_COMPANY_RISK"
    CLOSE_RISK = "CLOSE_RISK"
    RUN_MONITOR = "RUN_MONITOR"
    MAINTAIN_SOURCE = "MAINTAIN_SOURCE"
    IMPORT_MASTER = "IMPORT_MASTER"
    APPROVE_MASTER = "APPROVE_MASTER"
    MANAGE_RULE = "MANAGE_RULE"
    PUBLISH_MODEL = "PUBLISH_MODEL"
    EXPORT_RISK = "EXPORT_RISK"
    READ_AUDIT = "READ_AUDIT"


class AuthorizationDenied(PermissionError):
    """The authenticated principal cannot perform the requested action."""


class ResourceNotFound(LookupError):
    """A resource is hidden because it is outside the principal's scope."""


_ROLE_ACTIONS: dict[str, frozenset[Action]] = {
    GROUP_TAX_ROLE: frozenset(
        {
            Action.READ_RISK,
            Action.PROCESS_COMPANY_RISK,
            Action.CLOSE_RISK,
            Action.RUN_MONITOR,
            Action.APPROVE_MASTER,
            Action.MANAGE_RULE,
            Action.PUBLISH_MODEL,
            Action.EXPORT_RISK,
            Action.READ_AUDIT,
        }
    ),
    DIVISION_TAX_ROLE: frozenset({Action.READ_RISK}),
    COMPANY_FINANCE_ROLE: frozenset(
        {Action.READ_RISK, Action.PROCESS_COMPANY_RISK, Action.EXPORT_RISK}
    ),
    DATA_ADMIN_ROLE: frozenset({Action.MAINTAIN_SOURCE, Action.IMPORT_MASTER}),
    AUDIT_ROLE: frozenset({Action.READ_RISK, Action.READ_AUDIT}),
    MONITOR_SERVICE_ROLE: frozenset({Action.RUN_MONITOR}),
}


class PolicyEngine:
    """Evaluate action permissions before a repository or route is entered."""

    def actions_for(self, principal: Principal) -> frozenset[Action]:
        if principal.has_role(AUDIT_ROLE):
            return _ROLE_ACTIONS[AUDIT_ROLE]
        actions: set[Action] = set()
        for role in principal.roles:
            actions.update(_ROLE_ACTIONS.get(role, ()))
        if principal.is_service and not self._valid_service_scope(principal):
            actions.discard(Action.RUN_MONITOR)
        return frozenset(actions)

    def require(self, principal: Principal, action: Action) -> None:
        if action not in self.actions_for(principal):
            raise AuthorizationDenied(f"{principal.subject} cannot perform {action}")

    def company_scope(
        self,
        principal: Principal,
        action: Action,
        *,
        requested_company_id: UUID | None = None,
    ) -> frozenset[UUID] | None:
        self.require(principal, action)
        if principal.has_role(GROUP_TAX_ROLE) and not principal.is_service:
            return None

        scope = principal.allowed_company_ids
        if principal.is_service:
            assert principal.service_scope is not None
            scope = scope & principal.service_scope.company_ids
        if requested_company_id is not None and requested_company_id not in scope:
            raise ResourceNotFound(str(requested_company_id))
        return scope

    @staticmethod
    def _valid_service_scope(principal: Principal) -> bool:
        scope = principal.service_scope
        return bool(
            scope
            and scope.signature_verified
            and scope.queue.strip()
            and scope.run_type.strip()
            and scope.batch_id.strip()
            and scope.company_ids
            and scope.company_ids <= principal.allowed_company_ids
        )


DEFAULT_POLICY = PolicyEngine()


__all__ = [
    "Action",
    "AuthorizationDenied",
    "DEFAULT_POLICY",
    "PolicyEngine",
    "ResourceNotFound",
]

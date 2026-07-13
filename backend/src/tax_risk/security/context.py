"""Request and worker principal context applied to every unit of work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


_CURRENT_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "tax_risk_current_principal",
    default=None,
)


def current_principal() -> Principal | None:
    return _CURRENT_PRINCIPAL.get()


def bind_principal(principal: Principal) -> Token[Principal | None]:
    return _CURRENT_PRINCIPAL.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _CURRENT_PRINCIPAL.reset(token)


@contextmanager
def principal_context(principal: Principal) -> Iterator[Principal]:
    token = bind_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


def apply_principal_context(session: Session, principal: Principal) -> None:
    """Set RLS inputs for the current transaction without pool leakage."""

    company_scope = (
        "*"
        if principal.has_role(GROUP_TAX_ROLE) and not principal.is_service
        else ",".join(sorted(str(value) for value in principal.allowed_company_ids))
    )
    session.execute(
        text(
            "SELECT set_config('app.subject', :subject, true), "
            "set_config('app.roles', :roles, true), "
            "set_config('app.company_scope', :company_scope, true)"
        ),
        {
            "subject": principal.subject,
            "roles": ",".join(sorted(principal.roles)),
            "company_scope": company_scope,
        },
    )


__all__ = [
    "apply_principal_context",
    "bind_principal",
    "current_principal",
    "principal_context",
    "reset_principal",
]

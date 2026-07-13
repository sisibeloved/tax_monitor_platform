"""Database composition and transaction-local security context."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from tax_risk.persistence.models import Base
from tax_risk.persistence.repositories import engine, session_factory
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


def apply_principal_context(session: Session, principal: Principal) -> None:
    """Set RLS inputs for the current transaction without leaking through the pool."""

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

__all__ = ["Base", "apply_principal_context", "engine", "session_factory"]

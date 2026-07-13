from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tax_risk.db import apply_principal_context
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_transaction_local_security_context_is_cleared_on_pool_return(engine) -> None:
    company_id = uuid4()
    principal = Principal(
        subject="company-user",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/group/company",
    )

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with factory() as session:
        apply_principal_context(session, principal)
        values = session.execute(
            text(
                "SELECT current_setting('app.subject', true), "
                "current_setting('app.company_scope', true)"
            )
        ).one()
        assert values == ("company-user", str(company_id))
        session.commit()

    with factory() as session:
        values = session.execute(
            text(
                "SELECT current_setting('app.subject', true), "
                "current_setting('app.company_scope', true)"
            )
        ).one()
        assert values in (("", ""), (None, None))

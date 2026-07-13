from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import FastAPI, Request

from tax_risk.api.dependencies import get_principal
from tax_risk.security.context import current_principal
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_principal_dependency_resets_context_when_request_finishes() -> None:
    company_id = uuid4()
    principal = Principal(
        subject="company-user",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/companies/scoped",
    )
    app = FastAPI()
    app.state.principal_provider = lambda _request: principal
    request = Request({"type": "http", "app": app, "headers": []})

    async def exercise_dependency() -> None:
        dependency = get_principal(request)
        resolved = await anext(dependency)
        assert resolved == principal
        assert current_principal() == principal

        await dependency.aclose()
        assert current_principal() is None

    asyncio.run(exercise_dependency())

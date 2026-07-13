from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from tax_risk.security.service_scope import (
    ServiceScopeTokenError,
    issue_service_scope_token,
    verify_service_scope_token,
)


def test_signed_service_scope_round_trips_only_for_the_bound_task() -> None:
    batch_id = uuid4()
    companies = frozenset({uuid4(), uuid4()})
    token = issue_service_scope_token(
        secret="worker-scope-secret-for-unit-tests",
        queue="quarterly",
        run_type="QUARTERLY",
        batch_id=str(batch_id),
        company_ids=companies,
        period=date(2026, 6, 30),
    )

    scope = verify_service_scope_token(
        token,
        secret="worker-scope-secret-for-unit-tests",
        expected_queue="quarterly",
        expected_run_type="QUARTERLY",
        expected_batch_id=str(batch_id),
    )

    assert scope.company_ids == companies
    assert scope.signature_verified is True
    assert scope.period == date(2026, 6, 30)


def test_tampered_or_task_mismatched_service_scope_is_rejected() -> None:
    batch_id = uuid4()
    token = issue_service_scope_token(
        secret="worker-scope-secret-for-unit-tests",
        queue="exports",
        run_type="EXPORT",
        batch_id=str(batch_id),
        company_ids=frozenset({uuid4()}),
        period=date(2026, 6, 30),
    )

    for candidate, expected_batch in (
        (token + "x", str(batch_id)),
        (token, str(uuid4())),
    ):
        with pytest.raises(ServiceScopeTokenError):
            verify_service_scope_token(
                candidate,
                secret="worker-scope-secret-for-unit-tests",
                expected_queue="exports",
                expected_run_type="EXPORT",
                expected_batch_id=expected_batch,
            )

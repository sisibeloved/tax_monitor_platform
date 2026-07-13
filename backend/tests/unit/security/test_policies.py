from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from tax_risk.security.policies import (
    Action,
    AuthorizationDenied,
    PolicyEngine,
    ResourceNotFound,
)
from tax_risk.security.principal import (
    AUDIT_ROLE,
    COMPANY_FINANCE_ROLE,
    DATA_ADMIN_ROLE,
    DIVISION_TAX_ROLE,
    GROUP_TAX_ROLE,
    MONITOR_SERVICE_ROLE,
    Principal,
    ServiceScope,
)


def _principal(role: str, *company_ids):
    return Principal(
        subject=f"{role}-user",
        roles=frozenset({role}),
        allowed_company_ids=frozenset(company_ids),
        organization_path="/group/north",
    )


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (
            GROUP_TAX_ROLE,
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
            },
            {Action.MAINTAIN_SOURCE},
        ),
        (
            DIVISION_TAX_ROLE,
            {Action.READ_RISK},
            {Action.CLOSE_RISK, Action.MANAGE_RULE, Action.APPROVE_MASTER},
        ),
        (
            COMPANY_FINANCE_ROLE,
            {Action.READ_RISK, Action.PROCESS_COMPANY_RISK},
            {Action.CLOSE_RISK, Action.MANAGE_RULE, Action.PUBLISH_MODEL},
        ),
        (
            DATA_ADMIN_ROLE,
            {Action.MAINTAIN_SOURCE, Action.IMPORT_MASTER},
            {Action.READ_RISK, Action.CLOSE_RISK, Action.PUBLISH_MODEL},
        ),
        (
            AUDIT_ROLE,
            {Action.READ_RISK, Action.READ_AUDIT},
            {Action.PROCESS_COMPANY_RISK, Action.EXPORT_RISK, Action.MANAGE_RULE},
        ),
    ],
)
def test_role_action_matrix(role: str, allowed: set[Action], denied: set[Action]) -> None:
    policy = PolicyEngine()
    principal = _principal(role, uuid4())

    for action in allowed:
        policy.require(principal, action)
    for action in denied:
        with pytest.raises(AuthorizationDenied):
            policy.require(principal, action)


def test_company_scope_cannot_be_expanded_by_requested_filter() -> None:
    allowed = uuid4()
    forbidden = uuid4()
    policy = PolicyEngine()
    principal = _principal(COMPANY_FINANCE_ROLE, allowed)

    assert policy.company_scope(principal, Action.READ_RISK) == frozenset({allowed})
    assert policy.company_scope(
        principal, Action.READ_RISK, requested_company_id=allowed
    ) == frozenset({allowed})
    with pytest.raises(ResourceNotFound):
        policy.company_scope(
            principal,
            Action.READ_RISK,
            requested_company_id=forbidden,
        )


def test_only_group_tax_has_unrestricted_scope() -> None:
    policy = PolicyEngine()
    company_id = uuid4()

    assert policy.company_scope(
        _principal(GROUP_TAX_ROLE), Action.READ_RISK
    ) is None
    assert policy.company_scope(
        _principal(AUDIT_ROLE, company_id), Action.READ_AUDIT
    ) == frozenset({company_id})


def test_audit_role_keeps_a_mixed_role_principal_read_only() -> None:
    principal = Principal(
        subject="mixed-audit",
        roles=frozenset({AUDIT_ROLE, GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset({uuid4()}),
        organization_path="/group/audit",
    )

    policy = PolicyEngine()
    policy.require(principal, Action.READ_AUDIT)
    with pytest.raises(AuthorizationDenied):
        policy.require(principal, Action.PROCESS_COMPANY_RISK)


def test_monitor_service_requires_a_verified_frozen_scope() -> None:
    company_id = uuid4()
    service = Principal(
        subject="quarterly-worker",
        roles=frozenset({MONITOR_SERVICE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/services/quarterly",
        service_scope=ServiceScope(
            queue="quarterly",
            run_type="QUARTERLY",
            batch_id="batch-2026q2",
            company_ids=frozenset({company_id}),
            period=date(2026, 6, 30),
            signature_verified=True,
        ),
    )

    policy = PolicyEngine()
    policy.require(service, Action.RUN_MONITOR)
    assert policy.company_scope(service, Action.RUN_MONITOR) == frozenset({company_id})

    unsigned = Principal(
        subject=service.subject,
        roles=service.roles,
        allowed_company_ids=service.allowed_company_ids,
        organization_path=service.organization_path,
        service_scope=ServiceScope(
            queue="quarterly",
            run_type="QUARTERLY",
            batch_id="batch-2026q2",
            company_ids=frozenset({company_id}),
            period=date(2026, 6, 30),
            signature_verified=False,
        ),
    )
    with pytest.raises(AuthorizationDenied):
        policy.require(unsigned, Action.RUN_MONITOR)

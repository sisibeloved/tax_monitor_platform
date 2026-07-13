from __future__ import annotations

from uuid import uuid4

from tax_risk.application.audit import AuditEventDraft, AuditService
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


def test_audit_service_appends_structured_redacted_event(isolated_database_url) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    service = AuditService(lambda: UnitOfWork(factory))
    principal = Principal(
        subject="group-reviewer",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )

    event_id = service.append(
        AuditEventDraft(
            action="RISK_CASE_CLOSE",
            entity_type="RISK_CASE",
            entity_id=uuid4(),
            principal=principal,
            company_ids=frozenset({uuid4()}),
            result="SUCCEEDED",
            before_summary={"status": "GROUP_REVIEW", "reason": "自由文本"},
            after_summary={"status": "CLOSED"},
        )
    )

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT actor, action, before_summary, result FROM audit_event WHERE id = %s",
            (event_id,),
        ).one()
    assert row.actor == "group-reviewer"
    assert row.action == "RISK_CASE_CLOSE"
    assert row.before_summary["status"] == "GROUP_REVIEW"
    assert row.before_summary["reason"] != "自由文本"
    assert row.result == "SUCCEEDED"
    engine.dispose()


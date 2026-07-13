from pathlib import Path

from sqlalchemy import select

from tax_risk.application.audit import AuditService
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.persistence.risk_models import AuditEvent
from tax_risk.release.rollback import (
    DatabaseRollbackAuditSink,
    DeterministicDrillOperations,
    RollbackRunner,
)
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal

from .test_rollback_drill import _inputs


def test_rollback_events_reach_existing_immutable_audit_ledger(
    isolated_database_url: str,
    tmp_path: Path,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    audit_service = AuditService(lambda: UnitOfWork(factory))
    principal = Principal(
        subject="rollback-controller",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )
    sink = DatabaseRollbackAuditSink(audit_service, principal)
    try:
        report = RollbackRunner(
            DeterministicDrillOperations(),
            audit_sink=sink,
        ).run(_inputs(tmp_path))

        with factory() as session:
            actions = tuple(
                session.scalars(
                    select(AuditEvent.action)
                    .where(AuditEvent.entity_id == report.drill_id)
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            )
        assert actions[:2] == ("ROLLBACK_REQUESTED", "ROLLBACK_APPROVED")
        assert {
            "ROLLBACK_MANIFEST_SWITCHED",
            "ROLLBACK_CHECKSUM_VERIFIED",
            "ROLLBACK_REPRESENTATIVE_RERUN",
            "ROLLBACK_RECOVERY_DECIDED",
        } <= set(actions)
    finally:
        engine.dispose()

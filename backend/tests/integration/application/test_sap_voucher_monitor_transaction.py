from __future__ import annotations

from functools import partial

import pytest
from sqlalchemy import text

from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)


@pytest.mark.parametrize(
    "failure_stage",
    ["semantic_detection_persisted", "semantic_risk_case_persisted"],
)
def test_phase_3_routing_rolls_back_every_semantic_write(
    isolated_database_url: str,
    failure_stage: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, _, snapshot_id, _, sap_id, _ = _seed_graph(
            engine,
            account_family="DONATION",
        )
        with engine.connect() as connection:
            source_id = connection.execute(
                text(
                    "SELECT source_record_id FROM sap_expense_voucher_observation "
                    "WHERE id = :sap_id"
                ),
                {"sap_id": sap_id},
            ).scalar_one()

        def fail(stage: str) -> None:
            if stage == failure_stage:
                raise RuntimeError(f"injected failure: {stage}")

        router = SemanticCaseRouter(
            partial(UnitOfWork, factory),
            failure_injector=fail,
        )
        with pytest.raises(RuntimeError, match="injected failure"):
            router.route(
                _detection(
                    company_code=company_code,
                    snapshot_id=snapshot_id,
                    source_id=source_id,
                    label="SPONSORSHIP",
                    model_version="transaction-v1",
                    linked=True,
                    sap_observation_id=sap_id,
                    monitoring_type="DONATION",
                ),
                suspicious_labels=frozenset({"SPONSORSHIP"}),
            )

        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM semantic_detection_record "
                    " WHERE company_code = :company_code) AS detections, "
                    "(SELECT count(*) FROM semantic_evidence_task "
                    " WHERE company_code = :company_code) AS tasks, "
                    "(SELECT count(*) FROM risk_case AS r JOIN company AS c "
                    " ON c.id = r.company_id WHERE c.company_code = :company_code "
                    " AND r.monitor_type = 'DONATION') AS cases, "
                    "(SELECT count(*) FROM business_entertainment_case_detail AS d "
                    " JOIN semantic_detection_record AS s ON s.id = d.semantic_detection_id "
                    " WHERE s.company_code = :company_code) AS details, "
                    "(SELECT count(*) FROM review_action AS a JOIN risk_case AS r "
                    " ON r.id = a.risk_case_id JOIN company AS c ON c.id = r.company_id "
                    " WHERE c.company_code = :company_code) AS reviews, "
                    "(SELECT count(*) FROM audit_event AS a JOIN risk_case AS r "
                    " ON r.id = a.entity_id JOIN company AS c ON c.id = r.company_id "
                    " WHERE c.company_code = :company_code) AS audits"
                ),
                {"company_code": company_code},
            ).mappings().one()
        assert counts == {
            "detections": 0,
            "tasks": 0,
            "cases": 0,
            "details": 0,
            "reviews": 0,
            "audits": 0,
        }
    finally:
        engine.dispose()

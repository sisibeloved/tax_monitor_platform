from __future__ import annotations

from functools import partial
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)
from tax_risk.application.case_merge import (
    CaseMergeConflictError,
    CaseMergeNotFoundError,
    CaseMergeService,
)
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


SUSPICIOUS = frozenset({"MEETING_EXPENSE", "EMPLOYEE_EDUCATION", "EMPLOYEE_WELFARE"})


def test_resolve_revalidates_persisted_exact_evidence_and_is_idempotent(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, _, snapshot_id, source_id, _, exact_link_id = _seed_graph(engine)
        router = SemanticCaseRouter(partial(UnitOfWork, factory))
        routed = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="MEETING_EXPENSE",
                model_version="merge-v1",
            ),
            suspicious_labels=SUSPICIOUS,
        )
        assert routed.risk_case_id is not None
        service = CaseMergeService(partial(UnitOfWork, factory))

        with pytest.raises(CaseMergeNotFoundError):
            service.resolve_to_sap(
                business_case_id=routed.risk_case_id,
                evidence_link_id=uuid4(),
                expected_row_version=1,
                actor="reviewer",
            )

        with engine.begin() as connection:
            fuzzy_link_id = connection.execute(
                text(
                    """
                    INSERT INTO evidence_link (
                        company_code, source_record_id, target_record_id, relation_kind,
                        relation_quality, matched_field, snapshot_id
                    )
                    SELECT company_code, source_record_id, target_record_id,
                           'FUZZY_HINT', 'FUZZY', 'amount_date', snapshot_id
                    FROM evidence_link WHERE id = :link_id
                    RETURNING id
                    """
                ),
                {"link_id": exact_link_id},
            ).scalar_one()
        with pytest.raises(CaseMergeConflictError, match="EXACT"):
            service.resolve_to_sap(
                business_case_id=routed.risk_case_id,
                evidence_link_id=fuzzy_link_id,
                expected_row_version=1,
                actor="reviewer",
            )
        with pytest.raises(CaseMergeConflictError, match="row version"):
            service.resolve_to_sap(
                business_case_id=routed.risk_case_id,
                evidence_link_id=exact_link_id,
                expected_row_version=99,
                actor="reviewer",
            )

        merged = service.resolve_to_sap(
            business_case_id=routed.risk_case_id,
            evidence_link_id=exact_link_id,
            expected_row_version=1,
            actor="reviewer",
        )
        replay = service.resolve_to_sap(
            business_case_id=routed.risk_case_id,
            evidence_link_id=exact_link_id,
            expected_row_version=2,
            actor="reviewer",
        )

        assert merged.root_case_id == replay.root_case_id
        assert merged.source_case_id == routed.risk_case_id
        assert merged.merged is True
        with engine.connect() as connection:
            source = connection.execute(
                text(
                    "SELECT merged_into_case_id, row_version FROM risk_case WHERE id = :id"
                ),
                {"id": routed.risk_case_id},
            ).mappings().one()
            audit_count = connection.execute(
                text(
                    "SELECT count(*) FROM audit_event "
                    "WHERE entity_type = 'RISK_CASE' AND entity_id = :id "
                    "AND action = 'RESOLVE_TO_SAP'"
                ),
                {"id": routed.risk_case_id},
            ).scalar_one()
        assert source["merged_into_case_id"] == merged.root_case_id
        assert source["row_version"] == 2
        assert audit_count == 1
    finally:
        engine.dispose()

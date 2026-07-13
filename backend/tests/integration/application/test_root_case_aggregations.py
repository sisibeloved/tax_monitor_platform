from __future__ import annotations

from functools import partial

from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.application.case_merge import CaseMergeService
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


def test_all_consumers_count_only_unmerged_root_case_once(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, company_id, snapshot_id, source_id, _, link_id = _seed_graph(engine)
        router = SemanticCaseRouter(partial(UnitOfWork, factory))
        routed = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="MEETING_EXPENSE",
                model_version="report-v1",
            ),
            suspicious_labels=frozenset({"MEETING_EXPENSE"}),
        )
        assert routed.risk_case_id is not None
        merged = CaseMergeService(partial(UnitOfWork, factory)).resolve_to_sap(
            business_case_id=routed.risk_case_id,
            evidence_link_id=link_id,
            expected_row_version=1,
            actor="reviewer",
        )

        reporting = BusinessEntertainmentReportingService(partial(UnitOfWork, factory))
        rows = reporting.list_root_cases(company_scope=frozenset({company_id}))
        dashboard = reporting.summarize(company_scope=frozenset({company_id}))
        kpi = reporting.kpi(company_scope=frozenset({company_id}))
        source_audit = reporting.get_case_for_audit(
            routed.risk_case_id,
            company_scope=frozenset({company_id}),
        )

        assert len(rows) == 1
        assert rows[0].case_id == merged.root_case_id
        assert rows[0].risk_amount == 100
        assert rows[0].source_mode == "SAP_LINKED"
        assert dashboard.root_case_count == 1
        assert dashboard.total_risk_amount == 100
        assert dashboard.linked_count == 1 and dashboard.pending_location_count == 0
        assert kpi.risk_count == 1 and kpi.risk_amount == 100
        assert source_audit.merged_into_case_id == merged.root_case_id
    finally:
        engine.dispose()

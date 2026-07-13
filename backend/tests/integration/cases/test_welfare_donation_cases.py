from __future__ import annotations

from functools import partial

import pytest
from sqlalchemy import text

from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)


@pytest.mark.parametrize(
    ("monitoring_type", "label"),
    [("WELFARE", "BUSINESS_ENTERTAINMENT"), ("DONATION", "SPONSORSHIP")],
)
def test_rerun_upserts_the_same_sap_line_case(
    isolated_database_url: str,
    monitoring_type: str,
    label: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, _, snapshot_id, _, sap_id, _ = _seed_graph(
            engine,
            account_family=monitoring_type,
        )
        with engine.connect() as connection:
            source_id = connection.execute(
                text(
                    "SELECT source_record_id FROM sap_expense_voucher_observation "
                    "WHERE id = :sap_id"
                ),
                {"sap_id": sap_id},
            ).scalar_one()
        detection = _detection(
            company_code=company_code,
            snapshot_id=snapshot_id,
            source_id=source_id,
            label=label,
            model_version="case-v1",
            linked=True,
            sap_observation_id=sap_id,
            monitoring_type=monitoring_type,
        )
        router = SemanticCaseRouter(partial(UnitOfWork, factory))

        first = router.route(detection, suspicious_labels=frozenset({label}))
        replay = router.route(detection, suspicious_labels=frozenset({label}))

        assert first.risk_case_id == replay.risk_case_id
        detail = BusinessEntertainmentReportingService(
            partial(UnitOfWork, factory)
        ).get_case(first.risk_case_id, company_scope=None)
        assert detail.sap_fiscal_year == 2032
        assert detail.current_account_code == "660203"
        assert detail.current_account_name == "业务招待费"
        assert detail.signed_amount == 100
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT r.monitor_type::text, o.document_number, o.line_item "
                    "FROM risk_case AS r "
                    "JOIN business_entertainment_case_detail AS d ON d.risk_case_id = r.id "
                    "JOIN sap_expense_voucher_observation AS o ON o.id = d.sap_observation_id "
                    "WHERE r.id = :case_id"
                ),
                {"case_id": first.risk_case_id},
            ).one()
        assert row == (monitoring_type, "510001", "001")
    finally:
        engine.dispose()

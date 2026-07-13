from __future__ import annotations

from functools import partial
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)
from tax_risk.application.business_entertainment.export import escape_excel_text
from tax_risk.application.case_merge import CaseMergeService
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_export_contains_one_safe_root_case_row(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, company_id, snapshot_id, source_id, _, link_id = _seed_graph(engine)
        routed = SemanticCaseRouter(partial(UnitOfWork, factory)).route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="MEETING_EXPENSE",
                model_version="export-v1",
            ),
            suspicious_labels=frozenset({"MEETING_EXPENSE"}),
        )
        assert routed.risk_case_id is not None
        CaseMergeService(partial(UnitOfWork, factory)).resolve_to_sap(
            business_case_id=routed.risk_case_id,
            evidence_link_id=link_id,
            expected_row_version=1,
            actor="reviewer",
        )
        principal = Principal(
            subject="group-tax@example.com",
                roles=frozenset({COMPANY_FINANCE_ROLE}),
                allowed_company_ids=frozenset({company_id}),
                organization_path="/companies/scoped",
        )
        app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
        )
        response = TestClient(app).get("/api/v1/exports/business-entertainment.xlsx")

        assert response.status_code == 200
        workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
        try:
            rows = list(workbook.active.iter_rows(values_only=True))
        finally:
            workbook.close()
        assert len(rows) == 2
        assert rows[1][1] == company_code
        assert rows[1][4] == "SAP_LINKED"
        assert rows[1][8] == 100
        assert escape_excel_text("=1+1") == "'=1+1"
    finally:
        engine.dispose()

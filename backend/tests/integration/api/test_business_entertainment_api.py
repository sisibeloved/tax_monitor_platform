from __future__ import annotations

from functools import partial
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.application.test_semantic_case_routing import (
    _detection,
    _seed_graph,
)
from tax_risk.application.semantic.detection_router import SemanticCaseRouter
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


def test_business_entertainment_review_api_filters_details_coverage_and_resolves(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, company_id, snapshot_id, source_id, sap_id, link_id = _seed_graph(
            engine
        )
        routed = SemanticCaseRouter(partial(UnitOfWork, factory)).route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="MEETING_EXPENSE",
                model_version="api-v1",
            ),
            suspicious_labels=frozenset({"MEETING_EXPENSE"}),
        )
        assert routed.risk_case_id is not None
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sap_link_coverage (
                        company_code, period, sap_observation_id, document_number,
                        line_item, amount, currency, link_status,
                        exact_evidence_link_id, evaluated_via_business_document,
                        snapshot_id
                    ) VALUES (
                        :company_code, '2032-03-31', :sap_id, '510001', '001',
                        100, 'CNY', 'EXACT_LINKED', :link_id, true, :snapshot_id
                    )
                    """
                ),
                {
                    "company_code": company_code,
                    "sap_id": sap_id,
                    "link_id": link_id,
                    "snapshot_id": snapshot_id,
                },
            )

        principal = Principal(
            subject="finance-reviewer@example.com",
            roles=frozenset({COMPANY_FINANCE_ROLE}),
            allowed_company_ids=frozenset({company_id}),
            organization_path="/companies/scoped",
        )
        app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
        )
        client = TestClient(app)

        listed = client.get(
            "/api/v1/risk-cases",
            params={
                "monitoring_type": "BUSINESS_ENTERTAINMENT",
                "fiscal_year": 2032,
                "period": 3,
                "source_mode": "BUSINESS_DOCUMENT_UNLINKED",
                "sap_link_status": "PENDING_LOCATION",
                "confidence": "HIGH",
                "status": "NEW",
                "company": str(company_id),
            },
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1
        item = listed.json()["items"][0]
        assert item["id"] == str(routed.risk_case_id)
        assert item["source_mode"] == "BUSINESS_DOCUMENT_UNLINKED"
        assert item["sap_link_status"] == "PENDING_LOCATION"
        assert item["confidence_tier"] == "HIGH"
        assert item["period"] == 3

        detail = client.get(f"/api/v1/risk-cases/{routed.risk_case_id}")
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["canonical_source_record_id"] == str(source_id)
        assert detail_body["sap_document_number"] is None
        assert detail_body["risk_amount_source"] == "BUSINESS_DOCUMENT"
        assert detail_body["workflow_note"] == "待定位SAP凭证"
        assert detail_body["recommended_account_ids"] == ["MANUAL_REVIEW"]
        assert detail_body["model_version_id"] == "api-v1"
        assert detail_body["resolution_evidence_links"] == [
            {
                "evidence_link_id": str(link_id),
                "relation_quality": "EXACT",
                "matched_field": "reference",
                "sap_document_number": "510001",
                "sap_line_item": "001",
            }
        ]

        coverage = client.get(
            "/api/v1/business-entertainment/sap-link-coverage",
            params={"fiscal_year": 2032, "period": 3, "company": str(company_id)},
        )
        assert coverage.status_code == 200, coverage.text
        assert coverage.json()["total"] == 1
        assert coverage.json()["items"][0]["link_status"] == "EXACT_LINKED"
        assert coverage.json()["items"][0]["evaluated_via_business_document"] is True

        resolved = client.post(
            f"/api/v1/business-entertainment/risk-cases/{routed.risk_case_id}/resolve-to-sap",
            json={"evidence_link_id": str(link_id), "expected_row_version": 1},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["source_case_id"] == str(routed.risk_case_id)
        assert resolved.json()["root_case_id"] != str(routed.risk_case_id)

        stale = client.post(
            f"/api/v1/business-entertainment/risk-cases/{routed.risk_case_id}/resolve-to-sap",
            json={"evidence_link_id": str(link_id), "expected_row_version": 1},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "CASE_ROW_VERSION_CONFLICT"
    finally:
        engine.dispose()


def test_business_entertainment_api_hides_out_of_scope_resources(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, _, snapshot_id, source_id, _, link_id = _seed_graph(engine)
        routed = SemanticCaseRouter(partial(UnitOfWork, factory)).route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="EMPLOYEE_WELFARE",
                model_version="scope-v1",
            ),
            suspicious_labels=frozenset({"EMPLOYEE_WELFARE"}),
        )
        assert routed.risk_case_id is not None
        principal = Principal(
            subject="other-company@example.com",
            roles=frozenset({COMPANY_FINANCE_ROLE}),
            allowed_company_ids=frozenset({uuid4()}),
            organization_path="/companies/other",
        )
        client = TestClient(
            create_app(
                uow_factory=partial(UnitOfWork, factory),
                settings=Settings(environment="test"),
                principal_provider=lambda _request: principal,
            )
        )

        detail = client.get(f"/api/v1/risk-cases/{routed.risk_case_id}")
        resolve = client.post(
            f"/api/v1/business-entertainment/risk-cases/{routed.risk_case_id}/resolve-to-sap",
            json={"evidence_link_id": str(link_id), "expected_row_version": 1},
        )

        assert detail.status_code == 404
        assert resolve.status_code == 404
    finally:
        engine.dispose()

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


def test_business_entertainment_paths_are_idempotent_and_count_one_root_amount(
    e2e_database_url: str | None,
) -> None:
    assert e2e_database_url is not None
    engine, factory = create_session_factory(e2e_database_url)
    try:
        company_code, company_id, snapshot_id, source_id, _, exact_link_id = _seed_graph(
            engine
        )
        router = SemanticCaseRouter(partial(UnitOfWork, factory))

        unlinked_risk = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="EMPLOYEE_WELFARE",
                model_version="e2e-risk-v1",
            ),
            suspicious_labels=frozenset({"EMPLOYEE_WELFARE"}),
        )
        insufficient = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="INSUFFICIENT_EVIDENCE",
                model_version="e2e-evidence-v1",
            ),
            suspicious_labels=frozenset({"EMPLOYEE_WELFARE"}),
        )
        assert unlinked_risk.risk_case_id is not None
        assert insufficient.evidence_task_id is not None

        with engine.begin() as connection:
            batch_id = connection.execute(
                text("SELECT batch_id FROM source_record WHERE id = :source_id"),
                {"source_id": source_id},
            ).scalar_one()
            standalone_source_id = connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    ) VALUES (
                        :batch_id, :key, :company_id, 'sap_business_entertainment',
                        '2032-03-31', 'CNY', 2, 88, '{}'::jsonb, '{}'::jsonb, now()
                    ) RETURNING id
                    """
                ),
                {
                    "batch_id": batch_id,
                    "key": f"SAP-STANDALONE-{uuid4().hex}",
                    "company_id": company_id,
                },
            ).scalar_one()
            standalone_sap_id = connection.execute(
                text(
                    """
                    INSERT INTO sap_expense_voucher_observation (
                        source_record_id, ingest_batch_id, source_record_key,
                        company_code, fiscal_year, period, posting_date,
                        document_number, line_item, current_account_code,
                        current_account_name, amount, currency, summary, account_family
                    ) VALUES (
                        :source_id, :batch_id, :key, :company_code, 2032, 3,
                        '2032-03-20', '510099', '009', '660203', '业务招待费',
                        88, 'CNY', '无精确前置单据', 'BUSINESS_ENTERTAINMENT'
                    ) RETURNING id
                    """
                ),
                {
                    "source_id": standalone_source_id,
                    "batch_id": batch_id,
                    "key": f"SAP-OBS-{uuid4().hex}",
                    "company_code": company_code,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO sap_link_coverage (
                        company_code, period, sap_observation_id, document_number,
                        line_item, amount, currency, link_status,
                        exact_evidence_link_id, evaluated_via_business_document,
                        snapshot_id
                    ) VALUES (
                        :company_code, '2032-03-31', :sap_id, '510099', '009',
                        88, 'CNY', 'UNLINKED', NULL, false, :snapshot_id
                    )
                    """
                ),
                {
                    "company_code": company_code,
                    "sap_id": standalone_sap_id,
                    "snapshot_id": snapshot_id,
                },
            )

        principal = Principal(
            subject="e2e-finance@example.com",
            roles=frozenset({COMPANY_FINANCE_ROLE}),
            allowed_company_ids=frozenset({company_id}),
            organization_path="/companies/e2e",
        )
        client = TestClient(
            create_app(
                uow_factory=partial(UnitOfWork, factory),
                settings=Settings(environment="test"),
                principal_provider=lambda _request: principal,
            )
        )
        resolved = client.post(
            f"/api/v1/business-entertainment/risk-cases/"
            f"{unlinked_risk.risk_case_id}/resolve-to-sap",
            json={"evidence_link_id": str(exact_link_id), "expected_row_version": 1},
        )
        replay = client.post(
            f"/api/v1/business-entertainment/risk-cases/"
            f"{unlinked_risk.risk_case_id}/resolve-to-sap",
            json={"evidence_link_id": str(exact_link_id), "expected_row_version": 2},
        )
        risks = client.get(
            "/api/v1/risk-cases",
            params={
                "monitoring_type": "BUSINESS_ENTERTAINMENT",
                "fiscal_year": 2032,
                "period": 3,
            },
        )
        coverage = client.get(
            "/api/v1/business-entertainment/sap-link-coverage",
            params={"fiscal_year": 2032, "period": 3},
        )

        assert resolved.status_code == replay.status_code == 200
        assert resolved.json()["root_case_id"] == replay.json()["root_case_id"]
        assert risks.status_code == 200
        assert risks.json()["total"] == 1
        assert risks.json()["items"][0]["risk_amount"] == "100.000000000000"
        assert risks.json()["items"][0]["source_mode"] == "SAP_LINKED"
        assert coverage.status_code == 200
        assert coverage.json()["total"] == 1
        assert coverage.json()["items"][0]["link_status"] == "UNLINKED"
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM risk_case
                       WHERE monitor_type = 'BUSINESS_ENTERTAINMENT'
                         AND company_id = :company_id
                         AND merged_into_case_id IS NULL) AS roots,
                      (SELECT count(*) FROM semantic_evidence_task
                       WHERE company_code = :company_code) AS evidence_tasks,
                      (SELECT count(*) FROM audit_event
                       WHERE action = 'RESOLVE_TO_SAP'
                         AND entity_id = :source_case_id) AS merge_events
                    """
                ),
                {
                    "company_code": company_code,
                    "company_id": company_id,
                    "source_case_id": unlinked_risk.risk_case_id,
                },
            ).mappings().one()
        assert counts == {"roots": 1, "evidence_tasks": 1, "merge_events": 1}
    finally:
        engine.dispose()

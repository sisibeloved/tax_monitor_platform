from __future__ import annotations

from collections.abc import Callable
from functools import partial
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import (
    COMPANY_FINANCE_ROLE,
    GROUP_TAX_ROLE,
    Principal,
)


REFUND_TAX_YEAR = 2040
SCAN_YEAR = REFUND_TAX_YEAR + 1


def _principal(
    role: str,
    *,
    allowed_company_ids: frozenset[UUID] = frozenset(),
) -> Principal:
    return Principal(
        subject=f"refund-{role}@example.com",
        roles=frozenset({role}),
        allowed_company_ids=allowed_company_ids,
        organization_path="/GROUP/TAX" if role == GROUP_TAX_ROLE else "/COMPANY/FINANCE",
    )


def _seed_company(engine: Engine, label: str) -> tuple[str, UUID]:
    company_code = f"REFUND-API-{label}-{uuid4().hex}"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:company_code, :company_name, 'ACTIVE') RETURNING id"
            ),
            {"company_code": company_code, "company_name": f"Refund API {label}"},
        ).scalar_one()
    return company_code, company_id


def _target(
    company_code: str,
    amount: str,
    *,
    received_in_source: bool = False,
) -> dict[str, object]:
    return {
        "company_code": company_code,
        "source_record_key": f"api-row:{company_code}",
        "expected_refund_amount": amount,
        "raw_expected_refund_amount": amount,
        "currency": "CNY",
        "amount_scale": 2,
        "received_in_source": received_in_source,
    }


def _line(
    company_code: str,
    amount: str,
    document_number: str,
    *,
    account_category: str,
) -> dict[str, object]:
    return {
        "company_code": company_code,
        "client": "800",
        "ledger": "0L",
        "fiscal_year": SCAN_YEAR,
        "fiscal_period": 3,
        "posting_date": f"{SCAN_YEAR}-03-20",
        "document_number": document_number,
        "line_item": "001",
        "gl_account_code": {
            "INCOME_TAX_EXPENSE": "6801010000",
            "OTHER_INCOME": "6112010000",
            "TAXES_PAYABLE": "2221130000",
        }[account_category],
        "gl_account_name": {
            "INCOME_TAX_EXPENSE": "所得税费用",
            "OTHER_INCOME": "其他收益",
            "TAXES_PAYABLE": "应交税费-企业所得税",
        }[account_category],
        "account_category": account_category,
        "debit_credit": "CREDIT",
        "amount": amount,
        "currency": "CNY",
        "amount_scale": 2,
        "is_reversed": False,
    }


def _client(
    factory: sessionmaker[Session],
    principal: Principal,
    *,
    writeback_dispatcher: Callable[[], object] | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
            income_tax_refund_writeback_dispatcher=writeback_dispatcher,
        )
    )


def test_refund_api_import_scan_results_risk_cases_pagination_and_company_scope(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    correct = _seed_company(engine, "CORRECT")
    wrong_one = _seed_company(engine, "WRONG-1")
    wrong_two = _seed_company(engine, "WRONG-2")
    manual = _seed_company(engine, "MANUAL")
    company_codes = (correct[0], wrong_one[0], wrong_two[0])
    targets_payload = {
        "refund_tax_year": REFUND_TAX_YEAR,
        "source_version": "feishu-api-v1",
        "items": [
            _target(correct[0], "100.00"),
            _target(wrong_one[0], "200.00"),
            _target(wrong_two[0], "300.00"),
            _target(manual[0], "400.00", received_in_source=True),
        ],
    }
    evidence_payload = {
        "source_batch_key": "refund-api-march",
        "fiscal_year": SCAN_YEAR,
        "through_period": 3,
        "company_codes": company_codes,
        "items": [
            _line(correct[0], "100.00", "940001", account_category="INCOME_TAX_EXPENSE"),
            _line(wrong_one[0], "200.00", "940002", account_category="OTHER_INCOME"),
            _line(wrong_two[0], "300.00", "940003", account_category="TAXES_PAYABLE"),
        ],
    }
    scan_payload = {
        "refund_tax_year": REFUND_TAX_YEAR,
        "scan_year": SCAN_YEAR,
        "scan_month": 3,
        "source_batch_key": evidence_payload["source_batch_key"],
    }
    dispatch_calls: list[str] = []
    try:
        with _client(
            factory,
            _principal(GROUP_TAX_ROLE),
            writeback_dispatcher=lambda: dispatch_calls.append("dispatch"),
        ) as client:
            imported = client.post("/api/v1/income-tax-refunds/targets", json=targets_payload)
            target_replay = client.post(
                "/api/v1/income-tax-refunds/targets",
                json=targets_payload,
            )
            evidence = client.post(
                "/api/v1/income-tax-refunds/sap-evidence",
                json=evidence_payload,
            )
            evidence_replay = client.post(
                "/api/v1/income-tax-refunds/sap-evidence",
                json=evidence_payload,
            )
            scanned = client.post("/api/v1/income-tax-refunds/scans", json=scan_payload)
            scan_replay = client.post("/api/v1/income-tax-refunds/scans", json=scan_payload)
            results = client.get(
                "/api/v1/income-tax-refunds/results",
                params={
                    "refund_tax_year": REFUND_TAX_YEAR,
                    "scan_year": SCAN_YEAR,
                    "scan_month": 3,
                },
            )
            first_page = client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "period": 3,
                    "page": 1,
                    "page_size": 1,
                },
            )
            second_page = client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "quarter": 1,
                    "page": 2,
                    "page_size": 1,
                },
            )
            other_period = client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "period": 4,
                },
            )
            incompatible_semantic_filters = client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "period": 3,
                    "source_mode": "BUSINESS_DOCUMENT_UNLINKED",
                    "sap_link_status": "PENDING_LOCATION",
                    "confidence": "HIGH",
                },
            )
            wrong_year = client.post(
                "/api/v1/income-tax-refunds/scans",
                json=scan_payload | {"scan_year": SCAN_YEAR + 1},
            )
            outside_window = client.post(
                "/api/v1/income-tax-refunds/scans",
                json=scan_payload | {"scan_month": 2},
            )
            long_source_version = client.post(
                "/api/v1/income-tax-refunds/targets",
                json=targets_payload | {"source_version": "x" * 129},
            )

        assert imported.status_code == 201, imported.text
        assert imported.json() == {
            "source_version": "feishu-api-v1",
            "accepted_count": 4,
            "replayed_count": 0,
        }
        assert target_replay.status_code == 201, target_replay.text
        assert target_replay.json()["replayed_count"] == 4
        assert evidence.status_code == 201, evidence.text
        assert evidence.json()["accepted_count"] == 3
        assert evidence.json()["complete_company_count"] == 3
        assert evidence_replay.status_code == 201, evidence_replay.text
        assert evidence_replay.json()["replayed_count"] == 3
        assert scanned.status_code == 200, scanned.text
        assert scanned.json()["received_count"] == 4
        assert scanned.json()["wrong_account_count"] == 2
        assert scanned.json()["not_received_count"] == 0
        assert scan_replay.status_code == 200, scan_replay.text
        assert scan_replay.json() == scanned.json()
        assert dispatch_calls == ["dispatch", "dispatch"]
        assert results.status_code == 200, results.text
        assert results.json() == scanned.json()
        received_by_company = {item["company_code"]: item for item in results.json()["received"]}
        assert received_by_company[correct[0]]["account_family"] == "INCOME_TAX_EXPENSE"
        assert received_by_company[wrong_one[0]]["account_family"] == "OTHER_INCOME"
        assert received_by_company[wrong_two[0]]["account_family"] == "TAXES_PAYABLE"
        assert received_by_company[manual[0]]["receipt_source"] == "LARK_MANUAL"
        assert received_by_company[manual[0]]["booking_status"] == "NOT_APPLICABLE"
        assert received_by_company[manual[0]]["writeback_status"] is None

        assert first_page.status_code == 200, first_page.text
        assert first_page.json()["total"] == 2
        assert len(first_page.json()["items"]) == 1
        assert second_page.status_code == 200, second_page.text
        assert second_page.json()["total"] == 2
        assert len(second_page.json()["items"]) == 1
        listed_case_ids = {
            first_page.json()["items"][0]["id"],
            second_page.json()["items"][0]["id"],
        }
        assert len(listed_case_ids) == 2
        for item in (first_page.json()["items"][0], second_page.json()["items"][0]):
            assert item["monitoring_type"] == "INCOME_TAX_REFUND_ACCOUNT_ACCURACY"
            assert item["alert_code"] == "REFUND_BOOKED_TO_WRONG_ACCOUNT"
            assert item["fiscal_year"] == SCAN_YEAR
            assert item["period"] == 3
            assert item["source_mode"] == "SAP_LINE"
            assert item["sap_link_status"] == "LINKED"
            assert item["risk_amount"] in {"200.000000000000", "300.000000000000"}
        assert other_period.status_code == 200, other_period.text
        assert other_period.json()["total"] == 0
        assert incompatible_semantic_filters.status_code == 200
        assert incompatible_semantic_filters.json()["total"] == 0
        assert wrong_year.status_code == 422
        assert wrong_year.json()["detail"]["code"] == "INVALID_REFUND_SCAN_YEAR"
        assert outside_window.status_code == 422
        assert long_source_version.status_code == 422

        with _client(
            factory,
            _principal(
                COMPANY_FINANCE_ROLE,
                allowed_company_ids=frozenset({correct[1]}),
            ),
        ) as correct_client:
            scoped_results = correct_client.get(
                "/api/v1/income-tax-refunds/results",
                params={
                    "refund_tax_year": REFUND_TAX_YEAR,
                    "scan_year": SCAN_YEAR,
                    "scan_month": 3,
                },
            )
            scoped_cases = correct_client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "period": 3,
                },
            )
        assert scoped_results.status_code == 200, scoped_results.text
        assert scoped_results.json()["received_count"] == 1
        assert scoped_results.json()["received"][0]["company_id"] == str(correct[1])
        assert scoped_cases.status_code == 200, scoped_cases.text
        assert scoped_cases.json()["total"] == 0

        with _client(
            factory,
            _principal(
                COMPANY_FINANCE_ROLE,
                allowed_company_ids=frozenset({wrong_one[1]}),
            ),
        ) as wrong_client:
            scoped_wrong_cases = wrong_client.get(
                "/api/v1/risk-cases",
                params={
                    "monitoring_type": "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
                    "fiscal_year": SCAN_YEAR,
                    "period": 3,
                },
            )
        assert scoped_wrong_cases.status_code == 200, scoped_wrong_cases.text
        assert scoped_wrong_cases.json()["total"] == 1
        assert scoped_wrong_cases.json()["items"][0]["company_id"] == str(wrong_one[1])

        with engine.connect() as connection:
            persisted_counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM income_tax_refund_scan_result r "
                    " JOIN income_tax_refund_target t ON t.id = r.target_id "
                    " WHERE t.refund_tax_year = :refund_tax_year "
                    " AND t.company_id = ANY(:company_ids)), "
                    "(SELECT count(*) FROM income_tax_refund_writeback "
                    " WHERE company_id = ANY(:company_ids)), "
                    "(SELECT count(*) FROM risk_case "
                    " WHERE company_id = ANY(:company_ids) "
                    " AND monitor_type = 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY')"
                ),
                {
                    "refund_tax_year": REFUND_TAX_YEAR,
                    "company_ids": [correct[1], wrong_one[1], wrong_two[1], manual[1]],
                },
            ).one()
        assert persisted_counts == (3, 3, 2)
    finally:
        engine.dispose()

"""Fast in-process API/Celery contract test; this is not a deployed-service E2E."""

from __future__ import annotations

from functools import partial
from hashlib import sha256
import hmac
import json
import time
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.e2e.seed_quarterly_scenario import (
    QuarterlyScenarioSeed,
    ScenarioClient,
    seed_quarterly_scenario,
)
from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    build_quarterly_batch_canvas,
    register_quarterly_tasks,
)


DEV_SECRET = "e2e-quarterly-development-principal"
TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"}


def _principal_headers(secret: str, *, subject: str) -> dict[str, str]:
    payload = json.dumps(
        {
            "subject": subject,
            "roles": ["group-tax"],
            "allowed_company_ids": [],
            "organization_path": "/GROUP/TAX",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(secret.encode(), payload.encode(), sha256).hexdigest()
    return {
        "X-Development-Principal": payload,
        "X-Development-Principal-Signature": signature,
    }


def _start_and_poll(
    client: TestClient,
    *,
    seed: QuarterlyScenarioSeed,
    secret: str,
) -> tuple[dict[str, object], dict[str, object]]:
    headers = _principal_headers(secret, subject="e2e-group-tax-operator@example.com")
    response = client.post(
        "/api/v1/quarterly-runs",
        headers=headers,
        json={
            "fiscal_year": 2026,
            "quarter": 2,
            "snapshot_set_id": str(seed.snapshot_set_id),
            "rule_version": str(seed.rule_version_id),
        },
    )
    assert response.status_code == 202, response.text
    started = cast(dict[str, object], response.json())
    deadline = time.monotonic() + 90
    while True:
        response = client.get(
            f"/api/v1/quarterly-runs/{started['run_id']}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        terminal = cast(dict[str, object], response.json())
        if terminal["status"] in TERMINAL_STATUSES:
            return started, terminal
        if time.monotonic() >= deadline:
            raise AssertionError(f"quarterly run did not finish: {terminal}")
        time.sleep(0.1)


def _assert_standard_company_results(
    client: TestClient,
    *,
    seed: QuarterlyScenarioSeed,
    run_id: object,
    secret: str,
) -> None:
    headers = _principal_headers(secret, subject="e2e-group-tax-reader@example.com")
    response = client.get(
        "/api/v1/risk-cases",
        headers=headers,
        params={
            "fiscal_year": 2026,
            "quarter": 2,
            "company": str(seed.standard_company_id),
            "page_size": 10,
        },
    )
    assert response.status_code == 200, response.text
    cases = cast(dict[str, object], response.json())
    assert cases["total"] == 3
    items = cast(list[dict[str, object]], cases["items"])
    assert {item["run_id"] for item in items} == {str(run_id)}

    details: dict[str, dict[str, object]] = {}
    for item in items:
        detail_response = client.get(
            f"/api/v1/detections/{item['latest_detection_id']}",
            headers=headers,
        )
        assert detail_response.status_code == 200, detail_response.text
        detail = cast(dict[str, object], detail_response.json())
        details[str(detail["monitoring_type"])] = detail

    assert set(details) == {
        "ACCRUAL_ACCURACY",
        "TAX_BURDEN",
        "POTENTIAL_TAX_COST",
    }
    accrual = details["ACCRUAL_ACCURACY"]
    assert accrual["input_amount"] == "700000.000000000000"
    assert accrual["result_amount"] == "725000.000000000000"
    assert accrual["difference_amount"] == "25000.000000000000"
    assert accrual["alert_code"] == "UNDER_ACCRUED"

    burden = details["TAX_BURDEN"]
    assert burden["input_amount"] == "1625000.000000000000"
    assert burden["result_amount"] is None
    assert burden["tax_burden_rate"] == "0.032500000000"
    assert burden["tax_burden_deviation"] == "-0.057500000000"
    assert burden["alert_code"] == "TAX_BURDEN_LOW"

    potential = details["POTENTIAL_TAX_COST"]
    assert potential["input_amount"] == "1700000.000000000000"
    assert potential["result_amount"] == "2050000.000000000000"
    assert potential["difference_amount"] == "425000.000000000000"
    assert potential["alert_code"] == "POTENTIAL_TAX_COST"

    for detail in details.values():
        formula = cast(dict[str, object], detail["formula_substitution"])
        assert formula["cumulative_tax_payable"] == "1625000.00"
        assert formula["current_quarter_should_accrue"] == "725000.00"
        assert formula["current_quarter_difference"] == "25000.00"
        assert formula["current_tax_burden"] == "0.0325"
        assert formula["tax_burden_deviation"] == "-0.057500000000"
        assert formula["potential_adjustment"] == "1700000.000000000000"
        assert formula["potential_tax_payable"] == "2050000.00"
        assert formula["potential_tax_cost"] == "425000.00"


def test_in_process_eager_worker_contract_isolates_two_bad_companies(
    e2e_database_url: str | None,
) -> None:
    if e2e_database_url is None:
        pytest.skip("in-process eager contract requires the local isolated database")

    engine, factory = create_session_factory(e2e_database_url)
    uow_factory = partial(UnitOfWork, factory)
    settings = Settings.model_validate(
        {
            "environment": "development",
            "development_principal_enabled": True,
            "development_principal_secret": DEV_SECRET,
            "celery_task_always_eager": True,
            "celery_task_eager_propagates": False,
            "celery_task_store_eager_result": True,
            "quarterly_task_max_retries": 0,
        }
    )
    celery_app = create_celery_app(settings)

    def service_factory() -> QuarterlyBatchService:
        return QuarterlyBatchService(uow_factory)

    register_quarterly_tasks(app=celery_app, service_factory=service_factory)

    def dispatch(*, run_id: UUID, run_company_ids: tuple[UUID, ...]) -> None:
        build_quarterly_batch_canvas(
            app=celery_app,
            run_id=run_id,
            run_company_ids=run_company_ids,
        ).apply_async().get(timeout=90)

    app = create_app(
        uow_factory=uow_factory,
        settings=settings,
        quarterly_dispatcher=dispatch,
    )
    try:
        with TestClient(app) as client:
            seed = seed_quarterly_scenario(
                cast(ScenarioClient, client),
                engine,
                company_count=105,
                maker_headers=_principal_headers(
                    DEV_SECRET,
                    subject="e2e-group-tax-maker@example.com",
                ),
                reviewer_headers=_principal_headers(
                    DEV_SECRET,
                    subject="e2e-group-tax-reviewer@example.com",
                ),
                inject_blockers=True,
            )
            started, terminal = _start_and_poll(
                client,
                seed=seed,
                secret=DEV_SECRET,
            )

            assert started["dispatched_company_count"] == 105
            assert terminal["status"] == "PARTIAL_SUCCESS"
            assert terminal["requested_company_count"] == 105
            assert terminal["succeeded_company_count"] == 103
            assert terminal["blocked_company_count"] == 2
            assert terminal["failed_company_count"] == 0

            with engine.connect() as connection:
                blocked_rows = connection.execute(
                    text(
                        """
                        SELECT member.company_id, company_run.error_code
                        FROM monitoring_run_company AS company_run
                        JOIN snapshot_set_member AS member
                          ON member.id = company_run.snapshot_set_member_id
                        WHERE company_run.run_id = :run_id
                          AND company_run.status = 'BLOCKED'
                        """
                    ),
                    {"run_id": UUID(str(terminal["id"]))},
                ).all()
            blocked_by_company = {
                company_id: str(error_code)
                for company_id, error_code in blocked_rows
            }
            assert seed.ineffective_master_company_id is not None
            assert seed.inactive_company_id is not None
            assert blocked_by_company == {
                seed.ineffective_master_company_id: "TAX_MASTER_NOT_EFFECTIVE",
                seed.inactive_company_id: "COMPANY_NOT_CONTROLLED",
            }
            _assert_standard_company_results(
                client,
                seed=seed,
                run_id=terminal["id"],
                secret=DEV_SECRET,
            )
    finally:
        engine.dispose()

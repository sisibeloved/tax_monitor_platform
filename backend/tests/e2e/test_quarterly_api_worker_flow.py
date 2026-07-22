"""Deployed-service E2E using only HTTP plus controlled source-state injection."""

from __future__ import annotations

from hashlib import sha256
import hmac
import json
import os
import time
from typing import cast

import httpx
import pytest
from sqlalchemy import create_engine

from tests.e2e.seed_quarterly_scenario import (
    QuarterlyScenarioSeed,
    ScenarioClient,
    seed_quarterly_scenario,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("E2E_BASE_URL"),
    reason="requires E2E_BASE_URL, E2E_DATABASE_URL, a real broker, and a real worker",
)
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
    client: httpx.Client,
    *,
    seed: QuarterlyScenarioSeed,
    secret: str,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object]]:
    headers = _principal_headers(
        secret,
        subject="external-e2e-group-tax-operator@example.com",
    )
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
    assert started["dispatched_company_count"] == 105

    deadline = time.monotonic() + timeout_seconds
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
            raise AssertionError(
                f"real worker did not finish quarterly run before timeout: {terminal}"
            )
        time.sleep(0.5)


def _assert_standard_company_results(
    client: httpx.Client,
    *,
    seed: QuarterlyScenarioSeed,
    run_id: object,
    secret: str,
) -> None:
    headers = _principal_headers(
        secret,
        subject="external-e2e-group-tax-reader@example.com",
    )
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
    assert cases["total"] == 4
    items = cast(list[dict[str, object]], cases["items"])
    assert {item["run_id"] for item in items} == {str(run_id)}
    deferred_item = next(
        item for item in items if item["monitoring_type"] == "DEFERRED_TAX_ACCURACY"
    )
    assert deferred_item["input_amount"] == "2000000.000000000000"
    assert deferred_item["result_amount"] == "-1600000.000000000000"
    assert deferred_item["difference_amount"] == "-3600000.000000000000"
    list_formula = cast(dict[str, object], deferred_item["formula_substitution"])
    assert list_formula["loss_carryforward"] == "2000000.000000000000"
    assert list_formula["cumulative_profit"] == "10000000.000000000000"
    assert list_formula["sap_cumulative_deferred_tax_expense"] == (
        "2000000.000000000000"
    )

    details: dict[str, dict[str, object]] = {}
    for item in items:
        response = client.get(
            f"/api/v1/detections/{item['latest_detection_id']}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        detail = cast(dict[str, object], response.json())
        details[str(detail["monitoring_type"])] = detail

    assert set(details) == {
        "ACCRUAL_ACCURACY",
        "DEFERRED_TAX_ACCURACY",
        "TAX_BURDEN",
        "POTENTIAL_TAX_COST",
    }
    accrual = details["ACCRUAL_ACCURACY"]
    assert accrual["input_amount"] == "700000.000000000000"
    assert accrual["result_amount"] == "725000.000000000000"
    assert accrual["difference_amount"] == "25000.000000000000"
    assert accrual["alert_code"] == "UNDER_ACCRUED"

    deferred = details["DEFERRED_TAX_ACCURACY"]
    assert deferred["input_amount"] == "2000000.000000000000"
    assert deferred["result_amount"] == "-1600000.000000000000"
    assert deferred["difference_amount"] == "-3600000.000000000000"
    assert deferred["rate_value"] == "0.200000000000"
    assert deferred["alert_code"] == "DEFERRED_TAX_TO_REVERSE"
    assert deferred["direction"] == "REVERSE"

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
        assert formula["deferred_tax_rate"] == "0.200000000000"
        assert formula["sap_cumulative_deferred_tax_expense"] == (
            "2000000.000000000000"
        )
        assert formula["deferred_tax_base_formula"] == "LOSS_MINUS_PROFIT"
        assert formula["deferred_tax_base"] == "-8000000.000000000000"
        assert formula["system_cumulative_deferred_tax"] == "-1600000.00"
        assert formula["current_year_deferred_tax_adjustment"] == "-3600000.00"


def test_deployed_api_and_real_worker_process_unique_105_company_snapshot_set() -> None:
    base_url = os.environ["E2E_BASE_URL"].rstrip("/")
    database_url = os.getenv("E2E_DATABASE_URL")
    principal_secret = os.getenv("E2E_DEV_PRINCIPAL_SECRET")
    seed_token = os.getenv("E2E_SEED_TOKEN")
    assert database_url, "external E2E requires E2E_DATABASE_URL for the service database"
    assert principal_secret, "external E2E requires E2E_DEV_PRINCIPAL_SECRET"
    assert seed_token, "external E2E requires a unique E2E_SEED_TOKEN"
    timeout_seconds = float(os.getenv("E2E_WORKER_TIMEOUT_SECONDS", "300"))
    assert timeout_seconds > 0

    engine = create_engine(database_url)
    try:
        with httpx.Client(base_url=base_url, timeout=60) as client:
            seed = seed_quarterly_scenario(
                cast(ScenarioClient, client),
                engine,
                company_count=105,
                maker_headers=_principal_headers(
                    principal_secret,
                    subject="external-e2e-group-tax-maker@example.com",
                ),
                reviewer_headers=_principal_headers(
                    principal_secret,
                    subject="external-e2e-group-tax-reviewer@example.com",
                ),
                inject_blockers=True,
                token=seed_token,
            )
            print(f"E2E_STANDARD_COMPANY_CODE={seed.standard_company_code}")
            configured_company_code = os.getenv("E2E_STANDARD_COMPANY_CODE")
            if configured_company_code is not None:
                assert configured_company_code == seed.standard_company_code
            started, terminal = _start_and_poll(
                client,
                seed=seed,
                secret=principal_secret,
                timeout_seconds=timeout_seconds,
            )

            assert started["dispatched_company_count"] == 105
            assert terminal["status"] == "PARTIAL_SUCCESS"
            assert terminal["requested_company_count"] == 105
            assert terminal["succeeded_company_count"] == 103
            assert terminal["blocked_company_count"] == 2
            assert terminal["failed_company_count"] == 0
            _assert_standard_company_results(
                client,
                seed=seed,
                run_id=terminal["id"],
                secret=principal_secret,
            )
    finally:
        engine.dispose()

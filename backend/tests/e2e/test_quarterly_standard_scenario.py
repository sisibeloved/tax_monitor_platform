from __future__ import annotations

from decimal import Decimal
from functools import partial
from hashlib import sha256
import hmac
import json
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from seed_quarterly_scenario import ScenarioClient, seed_quarterly_scenario
from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


DEV_SECRET = "e2e-standard-development-principal"


def _principal_headers(subject: str) -> dict[str, str]:
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
    signature = hmac.new(DEV_SECRET.encode(), payload.encode(), sha256).hexdigest()
    return {
        "X-Development-Principal": payload,
        "X-Development-Principal-Signature": signature,
    }


def test_standard_company_calculates_exact_values_from_published_source_lineage(
    e2e_database_url: str | None,
) -> None:
    if e2e_database_url is None:
        pytest.skip("the standard calculation scenario is a local isolated-schema E2E")

    engine, factory = create_session_factory(e2e_database_url)
    uow_factory = partial(UnitOfWork, factory)
    app = create_app(
        uow_factory=uow_factory,
        settings=Settings(
            environment="development",
            development_principal_enabled=True,
            development_principal_secret=DEV_SECRET,
        ),
    )
    try:
        with TestClient(app) as client:
            seed = seed_quarterly_scenario(
                cast(ScenarioClient, client),
                engine,
                company_count=100,
                maker_headers=_principal_headers("e2e-standard-maker@example.com"),
                reviewer_headers=_principal_headers(
                    "e2e-standard-reviewer@example.com"
                ),
            )

        service = QuarterlyBatchService(uow_factory)
        plan = service.start_batch(
            fiscal_year=2026,
            quarter=2,
            snapshot_set_id=seed.snapshot_set_id,
            rule_version_id=seed.rule_version_id,
        )
        with engine.connect() as connection:
            run_company_id = connection.execute(
                text(
                    """
                    SELECT company_run.id
                    FROM monitoring_run_company AS company_run
                    JOIN snapshot_set_member AS member
                      ON member.id = company_run.snapshot_set_member_id
                    WHERE company_run.run_id = :run_id
                      AND member.company_id = :company_id
                    """
                ),
                {"run_id": plan.run_id, "company_id": seed.standard_company_id},
            ).scalar_one()

        outcome = service.run_company(
            run_company_id=run_company_id,
            task_id="e2e-standard-company",
        )

        assert outcome["status"] == "SUCCEEDED"
        with engine.connect() as connection:
            detections = list(
                connection.execute(
                    text(
                        """
                        SELECT *
                        FROM detection_record
                        WHERE run_id = :run_id AND company_id = :company_id
                        ORDER BY monitor_type
                        """
                    ),
                    {"run_id": plan.run_id, "company_id": seed.standard_company_id},
                ).mappings()
            )
            case_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM risk_case
                    WHERE company_id = :company_id
                    """
                ),
                {"company_id": seed.standard_company_id},
            ).scalar_one()

        assert len(detections) == 3
        assert case_count == 3
        by_type = {str(row["monitor_type"]): row for row in detections}

        accrual = by_type["ACCRUAL_ACCURACY"]
        assert accrual["input_amount"] == Decimal("700000.000000000000")
        assert accrual["result_amount"] == Decimal("725000.000000000000")
        assert accrual["difference_amount"] == Decimal("25000.000000000000")

        burden = by_type["TAX_BURDEN"]
        assert burden["input_amount"] == Decimal("1625000.000000000000")
        assert burden["tax_burden_rate"] == Decimal("0.032500000000")
        assert burden["tax_burden_deviation"] == Decimal("-0.057500000000")

        potential = by_type["POTENTIAL_TAX_COST"]
        assert potential["input_amount"] == Decimal("1700000.000000000000")
        assert potential["result_amount"] == Decimal("2050000.000000000000")
        assert potential["difference_amount"] == Decimal("425000.000000000000")
        assert potential["alert_code"] == "POTENTIAL_TAX_COST"

        formula = potential["formula_substitution"]
        assert formula["cumulative_tax_payable"] == "1625000.00"
        assert formula["current_quarter_should_accrue"] == "725000.00"
        assert formula["current_quarter_difference"] == "25000.00"
        assert formula["potential_adjustment"] == "1700000.000000000000"
        assert formula["potential_tax_payable"] == "2050000.00"
        assert formula["potential_tax_cost"] == "425000.00"

        lineage = potential["lineage"]
        assert lineage["company"]["id"] == str(seed.standard_company_id)
        assert lineage["snapshot"]["id"] == str(seed.standard_snapshot_id)
        assert lineage["tax_master_version"]["id"] == str(
            seed.tax_master_version_ids[0]
        )
        assert lineage["sources"][0]["batch"]["id"] == str(seed.sap_batch_id)
        assert len(lineage["metrics"]) == 8
    finally:
        engine.dispose()

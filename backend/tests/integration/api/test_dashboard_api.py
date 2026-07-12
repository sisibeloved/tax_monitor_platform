from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import hmac
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


DEV_PRINCIPAL_SECRET = "quarterly-api-development-secret"


def _principal_headers(
    *,
    roles: tuple[str, ...],
    allowed_company_ids: tuple[UUID, ...] = (),
) -> dict[str, str]:
    payload = json.dumps(
        {
            "subject": "dashboard-api-test@example.com",
            "roles": list(roles),
            "allowed_company_ids": [str(value) for value in allowed_company_ids],
            "organization_path": "/GROUP/TAX",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "X-Development-Principal": payload,
        "X-Development-Principal-Signature": hmac.new(
            DEV_PRINCIPAL_SECRET.encode(), payload.encode(), sha256
        ).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class _DashboardSeed:
    company_ids: tuple[UUID, UUID, UUID]
    company_codes: tuple[str, str, str]
    snapshot_set_id: UUID
    run_id: UUID
    potential_detection_id: UUID
    burden_detection_id: UUID


@pytest.fixture(scope="module")
def dashboard_api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine, _DashboardSeed]]:
    engine, factory = create_session_factory(isolated_database_url)
    token = uuid4().hex
    company_ids: list[UUID] = []
    company_codes: list[str] = []
    master_ids: list[UUID] = []
    snapshot_ids: list[UUID] = []
    member_ids: list[UUID] = []
    with engine.begin() as connection:
        rule_id = connection.execute(
            text(
                """
                SELECT id FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1' AND version = 'phase-1-reviewed'
                """
            )
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'TAX_MASTER_XLSX', :key, 'tax_master', 'SUCCEEDED', now(),
                    DATE '2033-09-30', 'FULL', 'dashboard-api', 'CNY', 2,
                    3, 3, 0, 0, repeat('d', 64)
                ) RETURNING id
                """
            ),
            {"key": f"dashboard-master-{token}"},
        ).scalar_one()
        set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:key, DATE '2033-09-30', 'DRAFT', 100)
                RETURNING id
                """
            ),
            {"key": f"dashboard-set-{token}"},
        ).scalar_one()
        for index in range(3):
            code = f"DASH-{token}-{index}"
            company_id = connection.execute(
                text(
                    """
                    INSERT INTO company (company_code, company_name)
                    VALUES (:code, :name) RETURNING id
                    """
                ),
                {"code": code, "name": f"Dashboard Company {index}"},
            ).scalar_one()
            master_id = connection.execute(
                text(
                    """
                    INSERT INTO tax_master_version (
                        company_id, source_batch_id, valid_from, version, status,
                        tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                        currency, amount_scale, source_checksum, source_row_number,
                        uploaded_by, data, published_at, approved_by
                    ) VALUES (
                        :company_id, :batch_id, DATE '2033-01-01', :version,
                        'PUBLISHED', 0.25, 0, 0.08, 'CNY', 2, repeat('e', 64),
                        :row_number, 'dashboard-maker', '{}'::jsonb,
                        now(), 'dashboard-reviewer'
                    ) RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "batch_id": batch_id,
                    "version": f"dashboard-v{index}",
                    "row_number": index + 2,
                },
            ).scalar_one()
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO accounting_snapshot (
                        company_id, tax_master_version_id, period,
                        source_version_set_hash, status, currency, amount_scale,
                        record_count, control_total, checksum, lineage, published_at
                    ) VALUES (
                        :company_id, :master_id, DATE '2033-09-30', :source_hash,
                        'PUBLISHED', 'CNY', 2, 8, 100, :checksum,
                        jsonb_build_object('metrics', '[]'::jsonb, 'sources', '[]'::jsonb),
                        now()
                    ) RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "master_id": master_id,
                    "source_hash": sha256(f"sources-{token}-{index}".encode()).hexdigest(),
                    "checksum": sha256(f"snapshot-{token}-{index}".encode()).hexdigest(),
                },
            ).scalar_one()
            member_id = connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                    VALUES (:set_id, :company_id, :snapshot_id) RETURNING id
                    """
                ),
                {"set_id": set_id, "company_id": company_id, "snapshot_id": snapshot_id},
            ).scalar_one()
            company_ids.append(company_id)
            company_codes.append(code)
            master_ids.append(master_id)
            snapshot_ids.append(snapshot_id)
            member_ids.append(member_id)

        run_id = connection.execute(
            text(
                """
                INSERT INTO monitoring_run (
                    run_key, run_type, snapshot_set_id, rule_version_id, status,
                    fiscal_year, quarter, requested_company_count,
                    succeeded_company_count, failed_company_count,
                    blocked_company_count, started_at, finished_at
                ) VALUES (
                    :key, 'QUARTERLY', :set_id, :rule_id, 'PARTIAL_SUCCESS',
                    2033, 3, 3, 1, 1, 1, now(), now()
                ) RETURNING id
                """
            ),
            {"key": f"dashboard-run-{token}", "set_id": set_id, "rule_id": rule_id},
        ).scalar_one()

        detection_ids: dict[str, UUID] = {}
        for monitor_type, calculation_status, result, difference, reason, alert, direction in (
            (
                "ACCRUAL_ACCURACY",
                "CALCULATED",
                "125",
                "25",
                None,
                "UNDER_ACCRUED",
                "UNDER",
            ),
            (
                "TAX_BURDEN",
                "NOT_CALCULABLE",
                None,
                None,
                "REVENUE_NON_POSITIVE",
                None,
                None,
            ),
            (
                "POTENTIAL_TAX_COST",
                "CALCULATED",
                "75",
                "-25",
                None,
                "POTENTIAL_TAX_COST",
                "DECREASE",
            ),
        ):
            detection_id = connection.execute(
                text(
                    """
                    INSERT INTO detection_record (
                        detection_key, run_id, company_id, snapshot_id,
                        rule_version_id, tax_master_version_id, monitor_type,
                        calculation_status, input_amount, result_amount,
                        difference_amount, rate_value, tax_burden_rate,
                        tax_burden_deviation, currency, amount_scale,
                        formula_substitution, lineage, structured_output,
                        not_calculated_reason, alert_code, direction
                    ) VALUES (
                        :key, :run_id, :company_id, :snapshot_id, :rule_id, :master_id,
                        :monitor_type, :calculation_status, 100, :result, :difference,
                        0.25, NULL, NULL, 'CNY', 2,
                        '{"base":"100.000000000000","rate":"0.250000000000"}'::jsonb,
                        jsonb_build_object(
                            'company', jsonb_build_object(
                                'id', CAST(:company_id_text AS text),
                                'company_code', CAST(:company_code AS text)),
                            'snapshot', jsonb_build_object(
                                'id', CAST(:snapshot_id_text AS text)),
                            'rule_version', jsonb_build_object(
                                'id', CAST(:rule_id_text AS text),
                                'version', 'phase-1-reviewed'),
                            'tax_master_version', jsonb_build_object(
                                'id', CAST(:master_id_text AS text),
                                'version', 'dashboard-v0'),
                            'sources', jsonb_build_array(jsonb_build_object('source', 'SAP')),
                            'metrics', '[]'::jsonb
                        ),
                        jsonb_build_object(
                            'monitor_type', CAST(:monitor_type_text AS text)),
                        :reason, :alert, :direction
                    ) RETURNING id
                    """
                ),
                {
                    "key": f"dashboard-detection-{token}-{monitor_type}",
                    "run_id": run_id,
                    "company_id": company_ids[0],
                    "company_id_text": str(company_ids[0]),
                    "company_code": company_codes[0],
                    "snapshot_id": snapshot_ids[0],
                    "snapshot_id_text": str(snapshot_ids[0]),
                    "rule_id": rule_id,
                    "rule_id_text": str(rule_id),
                    "master_id": master_ids[0],
                    "master_id_text": str(master_ids[0]),
                    "monitor_type": monitor_type,
                    "monitor_type_text": monitor_type,
                    "calculation_status": calculation_status,
                    "result": result,
                    "difference": difference,
                    "reason": reason,
                    "alert": alert,
                    "direction": direction,
                },
            ).scalar_one()
            detection_ids[monitor_type] = detection_id
            if alert is not None:
                connection.execute(
                    text(
                        """
                        INSERT INTO risk_case (
                            fingerprint, company_id, latest_detection_id, monitor_type,
                            status, risk_amount, risk_rate, currency, amount_scale,
                            risk_direction, priority, lineage
                        ) VALUES (
                            :fingerprint, :company_id, :detection_id, :monitor_type,
                            'NEW', 25, NULL, 'CNY', 2, :direction, 3, '{}'::jsonb
                        )
                        """
                    ),
                    {
                        "fingerprint": sha256(
                            f"{company_codes[0]}|2033|3|{monitor_type}".encode()
                        ).hexdigest(),
                        "company_id": company_ids[0],
                        "detection_id": detection_id,
                        "monitor_type": monitor_type,
                        "direction": direction,
                    },
                )

        for index, status in enumerate(("SUCCEEDED", "BLOCKED", "FAILED")):
            retryable = status == "FAILED"
            error_code = None
            error_message = None
            if status == "BLOCKED":
                error_code = "COMPANY_NOT_CONTROLLED"
                error_message = "company is inactive"
            elif status == "FAILED":
                error_code = "UNEXPECTED_COMPANY_FAILURE"
                error_message = "temporary worker failure"
            connection.execute(
                text(
                    """
                    INSERT INTO monitoring_run_company (
                        run_id, snapshot_set_id, snapshot_set_member_id,
                        status, attempt_count,
                        retryable, celery_task_id, started_at, finished_at,
                        error_code, error_message, detection_ids, case_ids
                    ) VALUES (
                        :run_id, :set_id, :member_id, :status, 1,
                        :retryable, :task_id,
                        now() - interval '1 minute', now(), :error_code, :error_message,
                        CAST(:detection_ids AS jsonb), CAST(:case_ids AS jsonb)
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "set_id": set_id,
                    "member_id": member_ids[index],
                    "status": status,
                    "retryable": retryable,
                    "task_id": f"dashboard-task-{index}",
                    "error_code": error_code,
                    "error_message": error_message,
                    "detection_ids": json.dumps(
                        [str(value) for value in detection_ids.values()]
                        if status == "SUCCEEDED"
                        else []
                    ),
                    "case_ids": json.dumps([]),
                },
            )

    settings = Settings.model_validate(
        {
            "environment": "development",
            "development_principal_enabled": True,
            "development_principal_secret": DEV_PRINCIPAL_SECRET,
        }
    )
    app = create_app(uow_factory=partial(UnitOfWork, factory), settings=settings)
    client = TestClient(app)
    seed = _DashboardSeed(
        company_ids=(company_ids[0], company_ids[1], company_ids[2]),
        company_codes=(company_codes[0], company_codes[1], company_codes[2]),
        snapshot_set_id=set_id,
        run_id=run_id,
        potential_detection_id=detection_ids["POTENTIAL_TAX_COST"],
        burden_detection_id=detection_ids["TAX_BURDEN"],
    )
    try:
        yield client, engine, seed
    finally:
        client.close()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM review_action WHERE risk_case_id IN "
                    "(SELECT id FROM risk_case WHERE company_id = ANY(:company_ids))"
                ),
                {"company_ids": company_ids},
            )
            connection.execute(
                text("DELETE FROM risk_case WHERE company_id = ANY(:company_ids)"),
                {"company_ids": company_ids},
            )
            connection.execute(
                text(
                    "ALTER TABLE detection_record "
                    "DISABLE TRIGGER trg_detection_record_immutable"
                )
            )
            connection.execute(
                text("DELETE FROM detection_record WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                text(
                    "ALTER TABLE detection_record "
                    "ENABLE TRIGGER trg_detection_record_immutable"
                )
            )
            connection.execute(
                text("DELETE FROM monitoring_run_company WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                text("DELETE FROM monitoring_run WHERE id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                text("DELETE FROM snapshot_set_member WHERE snapshot_set_id = :set_id"),
                {"set_id": set_id},
            )
            connection.execute(
                text("DELETE FROM snapshot_set WHERE id = :set_id"),
                {"set_id": set_id},
            )
            connection.execute(
                text(
                    "ALTER TABLE accounting_snapshot "
                    "DISABLE TRIGGER trg_accounting_snapshot_immutable"
                )
            )
            connection.execute(
                text("DELETE FROM accounting_snapshot WHERE id = ANY(:snapshot_ids)"),
                {"snapshot_ids": snapshot_ids},
            )
            connection.execute(
                text(
                    "ALTER TABLE accounting_snapshot "
                    "ENABLE TRIGGER trg_accounting_snapshot_immutable"
                )
            )
            connection.execute(
                text("DELETE FROM tax_master_version WHERE id = ANY(:master_ids)"),
                {"master_ids": master_ids},
            )
            connection.execute(
                text("DELETE FROM ingest_batch WHERE id = :batch_id"),
                {"batch_id": batch_id},
            )
            connection.execute(
                text("DELETE FROM company WHERE id = ANY(:company_ids)"),
                {"company_ids": company_ids},
            )
        engine.dispose()


def test_quarterly_dashboard_returns_scoped_counts_cost_and_company_pagination(
    dashboard_api_resources: tuple[TestClient, Engine, _DashboardSeed],
) -> None:
    client, _, seed = dashboard_api_resources
    group = client.get(
        "/api/v1/dashboard/quarterly?fiscal_year=2033&quarter=3&page=1&page_size=2",
        headers=_principal_headers(roles=("group-tax",)),
    )

    assert group.status_code == 200, group.text
    body = group.json()
    assert body["coverage_company_count"] == 3
    assert body["data_ready_count"] == 3
    assert body["blocked_count"] == 1
    assert body["risk_company_count"] == 1
    assert body["potential_tax_cost_total"] == "-25.000000000000"
    assert body["currency"] == "CNY"
    assert body["amount_scale"] == 2
    assert body["monitoring_type_counts"] == {
        "ACCRUAL_ACCURACY": 1,
        "TAX_BURDEN": 0,
        "POTENTIAL_TAX_COST": 1,
    }
    assert body["companies"]["total"] == 3
    assert body["companies"]["page"] == 1
    assert body["companies"]["page_size"] == 2
    assert len(body["companies"]["items"]) == 2
    assert {
        "company_id",
        "company_code",
        "company_name",
        "data_ready",
        "execution_status",
        "blocked_reason",
        "risk_count",
    } <= body["companies"]["items"][0].keys()

    finance = client.get(
        "/api/v1/dashboard/quarterly?fiscal_year=2033&quarter=3&page=1&page_size=20",
        headers=_principal_headers(
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[1],),
        ),
    )
    assert finance.status_code == 200, finance.text
    scoped = finance.json()
    assert scoped["coverage_company_count"] == 1
    assert scoped["data_ready_count"] == 1
    assert scoped["blocked_count"] == 1
    assert scoped["risk_company_count"] == 0
    assert scoped["potential_tax_cost_total"] == "0.000000000000"
    assert scoped["companies"]["total"] == 1
    assert scoped["companies"]["items"][0]["company_id"] == str(seed.company_ids[1])


def test_company_finance_reads_only_authorized_run_members_with_derived_summary(
    dashboard_api_resources: tuple[TestClient, Engine, _DashboardSeed],
) -> None:
    client, _, seed = dashboard_api_resources

    scoped = client.get(
        f"/api/v1/quarterly-runs/{seed.run_id}",
        headers=_principal_headers(
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[0], seed.company_ids[1]),
        ),
    )
    hidden = client.get(
        f"/api/v1/quarterly-runs/{seed.run_id}",
        headers=_principal_headers(
            roles=("company-finance",),
            allowed_company_ids=(uuid4(),),
        ),
    )

    assert scoped.status_code == 200, scoped.text
    body = scoped.json()
    assert body["snapshot_set_id"] == str(seed.snapshot_set_id)
    assert body["requested_company_count"] == 2
    assert body["succeeded_company_count"] == 1
    assert body["blocked_company_count"] == 1
    assert body["failed_company_count"] == 0
    assert body["status"] == "PARTIAL_SUCCESS"
    assert hidden.status_code == 404


def test_detection_detail_preserves_exact_values_lineage_and_not_calculable_reason(
    dashboard_api_resources: tuple[TestClient, Engine, _DashboardSeed],
) -> None:
    client, _, seed = dashboard_api_resources
    potential = client.get(
        f"/api/v1/detections/{seed.potential_detection_id}",
        headers=_principal_headers(roles=("group-tax",)),
    )
    burden = client.get(
        f"/api/v1/detections/{seed.burden_detection_id}",
        headers=_principal_headers(roles=("audit",)),
    )
    hidden = client.get(
        f"/api/v1/detections/{seed.potential_detection_id}",
        headers=_principal_headers(
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[1],),
        ),
    )

    assert potential.status_code == 200, potential.text
    detail = potential.json()
    assert detail["monitoring_type"] == "POTENTIAL_TAX_COST"
    assert detail["calculation_status"] == "CALCULATED"
    assert detail["input_amount"] == "100.000000000000"
    assert detail["result_amount"] == "75.000000000000"
    assert detail["difference_amount"] == "-25.000000000000"
    assert detail["rate_value"] == "0.250000000000"
    assert detail["currency"] == "CNY"
    assert detail["amount_scale"] == 2
    assert detail["not_calculated_reason"] is None
    assert detail["alert_code"] == "POTENTIAL_TAX_COST"
    assert detail["direction"] == "DECREASE"
    assert detail["formula_substitution"] == {
        "base": "100.000000000000",
        "rate": "0.250000000000",
    }
    assert detail["lineage"]["company"]["id"] == str(seed.company_ids[0])
    assert detail["lineage"]["rule_version"]["version"] == "phase-1-reviewed"

    assert burden.status_code == 200, burden.text
    not_calculable = burden.json()
    assert not_calculable["calculation_status"] == "NOT_CALCULABLE"
    assert not_calculable["result_amount"] is None
    assert not_calculable["difference_amount"] is None
    assert not_calculable["tax_burden_rate"] is None
    assert not_calculable["tax_burden_deviation"] is None
    assert not_calculable["not_calculated_reason"] == "REVENUE_NON_POSITIVE"
    assert hidden.status_code == 404

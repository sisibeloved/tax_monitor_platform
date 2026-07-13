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
    subject: str,
    roles: tuple[str, ...],
    allowed_company_ids: tuple[UUID, ...] = (),
) -> dict[str, str]:
    payload = json.dumps(
        {
            "subject": subject,
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
class _CaseSeed:
    company_ids: tuple[UUID, UUID]
    company_codes: tuple[str, str]
    case_ids: tuple[UUID, UUID]
    detection_ids: tuple[UUID, UUID]
    run_id: UUID


@pytest.fixture(scope="module")
def case_api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine, _CaseSeed]]:
    engine, factory = create_session_factory(isolated_database_url)
    token = uuid4().hex
    company_ids: list[UUID] = []
    company_codes: list[str] = []
    master_ids: list[UUID] = []
    snapshot_ids: list[UUID] = []
    case_ids: list[UUID] = []
    detection_ids: list[UUID] = []
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
                    DATE '2032-06-30', 'FULL', 'case-api', 'CNY', 2,
                    2, 2, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"case-api-batch-{token}"},
        ).scalar_one()
        set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
                VALUES (:key, DATE '2032-06-30', 'DRAFT', 100)
                RETURNING id
                """
            ),
            {"key": f"case-api-set-{token}"},
        ).scalar_one()
        run_id = connection.execute(
            text(
                """
                INSERT INTO monitoring_run (
                    run_key, run_type, snapshot_set_id, rule_version_id, status,
                    fiscal_year, quarter, requested_company_count,
                    succeeded_company_count, failed_company_count,
                    blocked_company_count, started_at, finished_at
                ) VALUES (
                    :key, 'QUARTERLY', :set_id, :rule_id, 'SUCCEEDED',
                    2032, 2, 2, 2, 0, 0, now(), now()
                ) RETURNING id
                """
            ),
            {"key": f"case-api-run-{token}", "set_id": set_id, "rule_id": rule_id},
        ).scalar_one()
        for index, direction in enumerate(("UNDER", "OVER")):
            company_code = f"CASE-API-{token}-{index}"
            company_id = connection.execute(
                text(
                    """
                    INSERT INTO company (company_code, company_name)
                    VALUES (:code, :name) RETURNING id
                    """
                ),
                {"code": company_code, "name": f"Case API Company {index}"},
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
                        :company_id, :batch_id, DATE '2032-01-01', :version,
                        'PUBLISHED', 0.25, 0, 0.08, 'CNY', 2, repeat('b', 64),
                        :row_number, 'case-maker', '{}'::jsonb, now(), 'case-reviewer'
                    ) RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "batch_id": batch_id,
                    "version": f"case-v{index}",
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
                        :company_id, :master_id, DATE '2032-06-30', repeat('c', 64),
                        'PUBLISHED', 'CNY', 2, 8, 100, :checksum,
                        '{}'::jsonb, now()
                    ) RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "master_id": master_id,
                    "checksum": (str(index) * 64),
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                    VALUES (:set_id, :company_id, :snapshot_id)
                    """
                ),
                {"set_id": set_id, "company_id": company_id, "snapshot_id": snapshot_id},
            )
            difference = "25" if direction == "UNDER" else "-25"
            detection_id = connection.execute(
                text(
                    """
                    INSERT INTO detection_record (
                        detection_key, run_id, company_id, snapshot_id,
                        rule_version_id, tax_master_version_id, monitor_type,
                        calculation_status, input_amount, result_amount,
                        difference_amount, rate_value, currency, amount_scale,
                        formula_substitution, lineage, structured_output,
                        alert_code, direction
                    ) VALUES (
                        :key, :run_id, :company_id, :snapshot_id, :rule_id, :master_id,
                        'ACCRUAL_ACCURACY', 'CALCULATED', 100, 125, :difference,
                        0.25, 'CNY', 2, '{"formula":"expected-actual"}'::jsonb,
                        jsonb_build_object('company', jsonb_build_object(
                            'id', CAST(:company_id_text AS text),
                            'company_code', CAST(:company_code AS text))),
                        '{}'::jsonb, :alert_code, :direction
                    ) RETURNING id
                    """
                ),
                {
                    "key": f"case-detection-{token}-{index}",
                    "run_id": run_id,
                    "company_id": company_id,
                    "company_id_text": str(company_id),
                    "company_code": company_code,
                    "snapshot_id": snapshot_id,
                    "rule_id": rule_id,
                    "master_id": master_id,
                    "difference": difference,
                    "alert_code": "UNDER_ACCRUED" if direction == "UNDER" else "OVER_ACCRUED",
                    "direction": direction,
                },
            ).scalar_one()
            case_id = connection.execute(
                text(
                    """
                    INSERT INTO risk_case (
                        fingerprint, company_id, latest_detection_id, monitor_type,
                        status, risk_amount, risk_rate, currency, amount_scale,
                        risk_direction, priority, lineage
                    ) VALUES (
                        :fingerprint, :company_id, :detection_id, 'ACCRUAL_ACCURACY',
                        'NEW', 25, NULL, 'CNY', 2, :direction, 3, '{}'::jsonb
                    ) RETURNING id
                    """
                ),
                {
                    "fingerprint": sha256(
                        f"{company_code}|2032|2|ACCRUAL_ACCURACY".encode()
                    ).hexdigest(),
                    "company_id": company_id,
                    "detection_id": detection_id,
                    "direction": direction,
                },
            ).scalar_one()
            company_ids.append(company_id)
            company_codes.append(company_code)
            master_ids.append(master_id)
            snapshot_ids.append(snapshot_id)
            case_ids.append(case_id)
            detection_ids.append(detection_id)

    settings = Settings.model_validate(
        {
            "environment": "development",
            "development_principal_enabled": True,
            "development_principal_secret": DEV_PRINCIPAL_SECRET,
        }
    )
    app = create_app(uow_factory=partial(UnitOfWork, factory), settings=settings)
    client = TestClient(app)
    seed = _CaseSeed(
        company_ids=(company_ids[0], company_ids[1]),
        company_codes=(company_codes[0], company_codes[1]),
        case_ids=(case_ids[0], case_ids[1]),
        detection_ids=(detection_ids[0], detection_ids[1]),
        run_id=run_id,
    )
    try:
        yield client, engine, seed
    finally:
        client.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM review_action WHERE risk_case_id = ANY(:case_ids)"),
                {"case_ids": list(case_ids)},
            )
            connection.execute(
                text("DELETE FROM risk_case WHERE id = ANY(:case_ids)"),
                {"case_ids": list(case_ids)},
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


def test_case_list_applies_group_company_and_audit_scope_in_sql(
    case_api_resources: tuple[TestClient, Engine, _CaseSeed],
) -> None:
    client, _, seed = case_api_resources
    group = client.get(
        "/api/v1/risk-cases?fiscal_year=2032&quarter=2&page=1&page_size=20",
        headers=_principal_headers(subject="group", roles=("group-tax",)),
    )
    filtered = client.get(
        "/api/v1/risk-cases?fiscal_year=2032&quarter=2"
        "&monitoring_type=ACCRUAL_ACCURACY&direction=UNDER&status=NEW",
        headers=_principal_headers(subject="group", roles=("group-tax",)),
    )
    finance = client.get(
        "/api/v1/risk-cases?fiscal_year=2032&quarter=2",
        headers=_principal_headers(
            subject="finance",
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[0],),
        ),
    )
    audit = client.get(
        "/api/v1/risk-cases?fiscal_year=2032&quarter=2",
        headers=_principal_headers(
            subject="audit",
            roles=("audit",),
            allowed_company_ids=seed.company_ids,
        ),
    )
    unauthorized_filter = client.get(
        f"/api/v1/risk-cases?fiscal_year=2032&quarter=2&company={seed.company_ids[1]}",
        headers=_principal_headers(
            subject="finance",
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[0],),
        ),
    )

    assert group.status_code == 200, group.text
    assert group.json()["total"] == 2
    assert group.json()["page"] == 1
    assert group.json()["page_size"] == 20
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    item = filtered.json()["items"][0]
    assert item["company_id"] == str(seed.company_ids[0])
    assert item["company_code"] == seed.company_codes[0]
    assert item["monitoring_type"] == "ACCRUAL_ACCURACY"
    assert item["risk_direction"] == "UNDER"
    assert item["risk_amount"] == "25.000000000000"
    assert item["risk_rate"] is None
    assert item["currency"] == "CNY"
    assert item["amount_scale"] == 2
    assert item["status"] == "NEW"
    assert item["latest_detection_id"] == str(seed.detection_ids[0])
    assert item["run_id"] == str(seed.run_id)
    assert item["calculation_status"] == "CALCULATED"
    assert item["input_amount"] == "100.000000000000"
    assert item["result_amount"] == "125.000000000000"
    assert item["difference_amount"] == "25.000000000000"
    assert item["tax_burden_rate"] is None
    assert item["tax_burden_deviation"] is None
    assert item["not_calculated_reason"] is None
    assert item["alert_code"] == "UNDER_ACCRUED"
    assert finance.status_code == 200
    assert finance.json()["total"] == 1
    assert finance.json()["items"][0]["company_id"] == str(seed.company_ids[0])
    assert audit.status_code == 200
    assert audit.json()["total"] == 2
    assert unauthorized_filter.status_code == 404


def test_case_actions_are_audited_and_hide_out_of_scope_cases(
    case_api_resources: tuple[TestClient, Engine, _CaseSeed],
) -> None:
    client, engine, seed = case_api_resources
    assigned = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[0]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "assign quarterly variance",
            "assignee": "case-owner@example.com",
            "attachment_refs": [],
        },
    )
    audit_write = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(subject="audit-reader", roles=("audit",)),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "not allowed",
            "assignee": "audit-cannot-assign@example.com",
        },
    )
    mixed_audit_write = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(
            subject="mixed-audit-reader",
            roles=("audit", "group-tax"),
        ),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "not allowed",
            "assignee": "mixed-audit-cannot-assign@example.com",
        },
    )
    mismatched_action = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "CLOSE",
            "to_status": "ASSIGNED",
            "reason": "action does not match the requested transition",
        },
    )
    unknown_action = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "NOT_A_REAL_ACTION",
            "to_status": "ASSIGNED",
            "reason": "unknown action",
        },
    )
    missing_assignee = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[0]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "assignment must name its owner",
        },
    )
    blank_assignee = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[0]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "assignment must name its owner",
            "assignee": "   ",
        },
    )
    non_assign_with_assignee = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={
            "action": "CLOSE",
            "to_status": "ASSIGNED",
            "reason": "only assignment can name an owner",
            "assignee": "should-not-be-stored@example.com",
        },
    )
    hidden = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(
            subject="finance",
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[0],),
        ),
        json={
            "action": "ASSIGN",
            "to_status": "ASSIGNED",
            "reason": "hidden",
            "assignee": "hidden-owner@example.com",
        },
    )

    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["id"] == str(seed.case_ids[0])
    assert assigned.json()["status"] == "ASSIGNED"
    assert assigned.json()["assignee"] == "case-owner@example.com"
    assert assigned.json()["row_version"] == 2
    assert audit_write.status_code == 403
    assert mixed_audit_write.status_code == 403
    assert mismatched_action.status_code == 409
    assert mismatched_action.json()["detail"]["code"] == "ACTION_TRANSITION_MISMATCH"
    assert unknown_action.status_code == 422
    assert missing_assignee.status_code == 422
    assert blank_assignee.status_code == 422
    assert non_assign_with_assignee.status_code == 422
    assert hidden.status_code == 404
    with engine.connect() as connection:
        action = connection.execute(
            text(
                """
                SELECT actor, actor_role, from_status, action, to_status, reason, assignee
                FROM review_action WHERE risk_case_id = :case_id
                """
            ),
            {"case_id": seed.case_ids[0]},
        ).mappings().one()
    assert action == {
        "actor": "group-reviewer",
        "actor_role": "group-tax",
        "from_status": "NEW",
        "action": "ASSIGN",
        "to_status": "ASSIGNED",
        "reason": "assign quarterly variance",
        "assignee": "case-owner@example.com",
    }


def test_only_group_tax_can_close_a_case(
    case_api_resources: tuple[TestClient, Engine, _CaseSeed],
) -> None:
    client, engine, seed = case_api_resources
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE risk_case
                SET status = 'GROUP_REVIEW', row_version = row_version + 1
                WHERE id = :case_id
                """
            ),
            {"case_id": seed.case_ids[1]},
        )

    finance = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(
            subject="company-finance",
            roles=("company-finance",),
            allowed_company_ids=(seed.company_ids[1],),
        ),
        json={"action": "CLOSE", "to_status": "CLOSED", "reason": "finance close"},
    )
    group = client.post(
        f"/api/v1/risk-cases/{seed.case_ids[1]}/actions",
        headers=_principal_headers(subject="group-reviewer", roles=("group-tax",)),
        json={"action": "CLOSE", "to_status": "CLOSED", "reason": "review passed"},
    )

    assert finance.status_code == 403
    assert group.status_code == 200, group.text
    assert group.json()["status"] == "CLOSED"

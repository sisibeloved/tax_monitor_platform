from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from functools import partial
import json
import re
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from httpx import Response
from sqlalchemy import Engine, event, text
from starlette.requests import Request

from tax_risk.api.routes.snapshots import router
from tax_risk.application.snapshots import (
    MAX_SNAPSHOT_SET_MEMBERS,
    MAX_SNAPSHOT_SOURCE_BATCHES,
    REQUIRED_QUARTERLY_METRICS,
)
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import Principal


PERIOD = date(2026, 6, 30)
GROUP_TAX_ADMIN = Principal(
    subject="group-tax-admin@example.com",
    roles=frozenset({"group-tax"}),
    allowed_company_ids=frozenset(),
    organization_path="/GROUP/TAX",
)


def _group_tax_admin(_request: Request) -> Principal:
    return GROUP_TAX_ADMIN


@pytest.fixture
def api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
    )
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def _seed_api_case(engine: Engine, *, with_master: bool = True) -> tuple[str, UUID]:
    token = uuid4().hex
    company_code = f"API-SNAPSHOT-{token}"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:code, :name)
                RETURNING id
                """
            ),
            {"code": company_code, "name": f"API company {token}"},
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'SAP', :key, 'quarterly_metric', 'SUCCEEDED', now(),
                    :period, 'FULL', 'api-v1', 'CNY', 2, 8, 8, 0, 36,
                    :checksum
                ) RETURNING id
                """
            ),
            {
                "key": f"api-batch-{token}",
                "period": PERIOD,
                "checksum": token.ljust(64, "a")[:64],
            },
        ).scalar_one()
        for index, metric in enumerate(REQUIRED_QUARTERLY_METRICS, start=1):
            record_key = f"{metric}-{token}"
            payload = {
                "source_record_key": record_key,
                "company_code": company_code,
                "dataset_code": "quarterly_metric",
                "period": PERIOD.isoformat(),
                "currency": "CNY",
                "amount_scale": 2,
                "metric_code": metric,
                "amount": str(index),
            }
            connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    ) VALUES (
                        :batch_id, :key, :company_id, 'quarterly_metric', :period,
                        'CNY', 2, :amount, CAST(:payload AS jsonb),
                        CAST(:lineage AS jsonb), now()
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "key": record_key,
                    "company_id": company_id,
                    "period": PERIOD,
                    "amount": index,
                    "payload": json.dumps(payload),
                    "lineage": json.dumps({"row_number": index + 1}),
                },
            )
        if with_master:
            master_batch = connection.execute(
                text(
                    """
                    INSERT INTO ingest_batch (
                        source, source_batch_key, dataset_code, status, extraction_time,
                        period, mode, schema_version, currency, amount_scale,
                        record_count, accepted_count, rejected_count, control_total, checksum
                    ) VALUES (
                        'TAX_MASTER_XLSX', :key, 'tax_master', 'SUCCEEDED', now(),
                        :period, 'FULL', 'api-master-v1', 'CNY', 2, 1, 1, 0, 0,
                        :checksum
                    ) RETURNING id
                    """
                ),
                {
                    "key": f"api-master-{token}",
                    "period": PERIOD,
                    "checksum": ("m" + token).ljust(64, "b")[:64],
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO tax_master_version (
                        company_id, source_batch_id, valid_from, version, status,
                        tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                        currency, amount_scale, source_file_name, source_checksum,
                        source_row_number, uploaded_by, data, published_at, approved_by
                    ) VALUES (
                        :company_id, :batch_id, DATE '2026-01-01', 'v1', 'PUBLISHED',
                        0.25, 0, 0.08, 'CNY', 2, 'api.xlsx', :checksum, 2,
                        'maker', '{}'::jsonb, now(), 'reviewer'
                    )
                    """
                ),
                {
                    "company_id": company_id,
                    "batch_id": master_batch,
                    "checksum": ("v" + token).ljust(64, "c")[:64],
                },
            )
    return company_code, batch_id


def _validate(client: TestClient, company_code: str, batch_id: UUID) -> Response:
    return client.post(
        "/api/v1/snapshots/validate",
        json={
            "company_code": company_code,
            "period": PERIOD.isoformat(),
            "source_batch_ids": [str(batch_id)],
            "accepted_partial_batch_ids": [],
        },
    )


def _create_public_batch(
    client: TestClient,
    *,
    dataset_code: str,
    source: str,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/ingest-batches",
        json={
            "source": source,
            "source_batch_key": f"{dataset_code}-{uuid4().hex}",
            "dataset_code": dataset_code,
            "extraction_time": "2026-07-01T08:00:00Z",
            "period": PERIOD.isoformat(),
            "mode": "FULL",
            "schema_version": "1",
            "currency": "CNY",
            "amount_scale": 2,
            "source_primary_key_definition": {"fields": ["source_record_key"]},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_public_csv(client: TestClient, batch_id: object, payload: bytes) -> Response:
    return client.post(
        f"/api/v1/ingest-batches/{batch_id}/files",
        files={"file": ("source.csv", payload, "text/csv")},
    )


def _seed_public_tax_master(engine: Engine, company_code: str) -> None:
    token = uuid4().hex
    with engine.begin() as connection:
        company_id, company_name = connection.execute(
            text(
                "SELECT id, company_name FROM company WHERE company_code = :company_code"
            ),
            {"company_code": company_code},
        ).one()
        source_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'TAX_MASTER_XLSX', :key, 'tax_master', 'SUCCEEDED', now(),
                    :period, 'FULL', '1', 'CNY', 2, 1, 1, 0, 0, :checksum
                ) RETURNING id
                """
            ),
            {
                "key": f"partial-master-{token}",
                "period": PERIOD,
                "checksum": token.ljust(64, "a")[:64],
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, source_file_name, source_checksum,
                    source_row_number, uploaded_by, data, published_at, approved_by
                ) VALUES (
                    :company_id, :source_batch_id, DATE '2026-01-01', 'v1',
                    'PUBLISHED', 0.25, 0, 0.08, 'CNY', 2, 'master.xlsx',
                    :checksum, 2, 'maker',
                    jsonb_build_object('company_name', CAST(:company_name AS text)),
                    now(), 'reviewer'
                )
                """
            ),
            {
                "company_id": company_id,
                "source_batch_id": source_batch_id,
                "checksum": ("m" + token).ljust(64, "b")[:64],
                "company_name": company_name,
            },
        )


def test_snapshot_router_exposes_validate_publish_and_set_routes() -> None:
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if isinstance(route, APIRoute)
    }

    assert ("/api/v1/snapshots/validate", ("POST",)) in paths
    assert ("/api/v1/snapshots/{snapshot_id}/publish", ("POST",)) in paths
    assert ("/api/v1/snapshot-sets", ("POST",)) in paths


def test_real_partial_ingest_evidence_can_be_explicitly_accepted_by_snapshot_api(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    target_code = f"TARGET-{uuid4().hex}"
    other_code = f"OTHER-{uuid4().hex}"
    company_batch = _create_public_batch(
        client,
        dataset_code="company_master",
        source="COMPANY_REGISTRY",
    )
    company_payload = (
        "source_record_key,company_code,company_name,lifecycle,extracted_at\n"
        f"target,{target_code},Target Company,ACTIVE,2026-07-01T08:00:00+00:00\n"
        f"other,{other_code},Other Company,ACTIVE,2026-07-01T08:00:00+00:00\n"
    ).encode()
    company_result = _upload_public_csv(client, company_batch["id"], company_payload)
    assert company_result.status_code == 200, company_result.text
    assert company_result.json()["status"] == "SUCCEEDED"
    _seed_public_tax_master(engine, target_code)

    financial_batch = _create_public_batch(
        client,
        dataset_code="quarterly_metric",
        source="SAP",
    )
    rows = [
        (
            f"target-{index},{target_code},2026,{PERIOD.isoformat()},CNY,2,"
            f"{metric},{index},2026-07-01T08:00:00+00:00"
        )
        for index, metric in enumerate(REQUIRED_QUARTERLY_METRICS, start=1)
    ]
    rows.append(
        f"other-invalid,{other_code},2026,{PERIOD.isoformat()},CNY,2,"
        "other_metric,not-a-decimal,2026-07-01T08:00:00+00:00"
    )
    financial_payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n" + "\n".join(rows) + "\n"
    ).encode()
    uploaded = _upload_public_csv(client, financial_batch["id"], financial_payload)

    assert uploaded.status_code == 200, uploaded.text
    batch = uploaded.json()
    assert batch["status"] == "PARTIAL"
    assert batch["accepted_count"] == 8
    assert batch["rejected_count"] == 1
    assert batch["errors"][0]["details"] == {
        "company_code": other_code,
        "metric_code": "other_metric",
        "field": "amount",
        "rejected_value": "not-a-decimal",
    }
    request = {
        "company_code": target_code,
        "period": PERIOD.isoformat(),
        "source_batch_ids": [str(financial_batch["id"])],
    }
    unaccepted = client.post("/api/v1/snapshots/validate", json=request)
    accepted = client.post(
        "/api/v1/snapshots/validate",
        json=request | {"accepted_partial_batch_ids": [str(financial_batch["id"])]},
    )

    assert unaccepted.status_code == 200
    assert unaccepted.json()["valid"] is False
    assert "SOURCE_NOT_READY" in {
        issue["error_code"] for issue in unaccepted.json()["issues"]
    }
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["valid"] is True, accepted.text
    evidence = accepted.json()["snapshot"]["lineage"]["sources"][0][
        "partial_decision"
    ]["evidence"][0]
    assert evidence["classification"] == "OTHER_COMPANY"
    assert evidence["details"] == batch["errors"][0]["details"]


def test_validate_and_publish_api_returns_frozen_snapshot_and_utc_z(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)

    validated = _validate(client, company_code, batch_id)

    assert validated.status_code == 200, validated.text
    body = validated.json()
    assert body["valid"] is True
    assert body["issues"] == []
    assert body["reused"] is False
    assert body["snapshot"]["status"] == "DRAFT"
    assert len(body["snapshot"]["lineage"]["metrics"]) == 8
    assert body["snapshot"]["control_total"] == "36.000000000000"

    replay = _validate(client, company_code, batch_id)
    published = client.post(
        f"/api/v1/snapshots/{body['snapshot']['id']}/publish"
    )

    assert replay.status_code == 200
    assert replay.json()["reused"] is True
    assert replay.json()["snapshot"]["id"] == body["snapshot"]["id"]
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"
    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z",
        published.json()["published_at"],
    )
    assert "data_ready_at" not in published.json()


def test_quality_failure_is_200_valid_false_with_stable_issue_contract(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine, with_master=False)

    response = _validate(client, company_code, batch_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["snapshot"] is None
    issue = next(
        item for item in body["issues"] if item["error_code"] == "TAX_MASTER_MISSING"
    )
    assert issue == {
        "category": "DATA_QUALITY",
        "error_code": "TAX_MASTER_MISSING",
        "source": "tax_master",
        "field": "effective_version",
        "company": company_code,
        "period": PERIOD.isoformat(),
        "remediation": "Publish one tax master version effective on the quarter end.",
    }


def test_validate_noncanonical_source_payload_is_data_quality_not_500(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE source_record
                SET payload = jsonb_set(payload, '{extra_decimal}', '1.25'::jsonb)
                WHERE id = (
                    SELECT id FROM source_record
                    WHERE batch_id = :batch_id
                    ORDER BY id LIMIT 1
                )
                """
            ),
            {"batch_id": batch_id},
        )

    response = _validate(client, company_code, batch_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["snapshot"] is None
    assert any(
        issue["error_code"] == "NON_CANONICAL_JSON"
        and issue["field"] == "source_record.payload"
        for issue in body["issues"]
    )
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT count(*)
                FROM accounting_snapshot AS snapshot
                JOIN company ON company.id = snapshot.company_id
                WHERE company.company_code = :company_code
                """
            ),
            {"company_code": company_code},
        ).scalar_one() == 0


@pytest.mark.parametrize("json_value", ["null", "[]", '"text"', "7"])
@pytest.mark.parametrize("column", ["payload", "lineage"])
def test_validate_nonobject_source_json_is_data_quality_not_500(
    api_resources: tuple[TestClient, Engine],
    column: str,
    json_value: str,
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)
    assert column in {"payload", "lineage"}
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                UPDATE source_record
                SET {column} = CAST(:json_value AS jsonb)
                WHERE id = (
                    SELECT id FROM source_record
                    WHERE batch_id = :batch_id
                    ORDER BY id LIMIT 1
                )
                """
            ),
            {"batch_id": batch_id, "json_value": json_value},
        )

    response = _validate(client, company_code, batch_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["snapshot"] is None
    assert any(
        issue["error_code"] == "NON_CANONICAL_JSON"
        and issue["field"] == f"source_record.{column}"
        for issue in body["issues"]
    )


def test_publish_noncanonical_source_lineage_is_422_and_leaves_draft(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)
    validated = _validate(client, company_code, batch_id)
    assert validated.status_code == 200 and validated.json()["valid"] is True
    snapshot_id = validated.json()["snapshot"]["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE source_record
                SET lineage = jsonb_set(lineage, '{confidence}', '0.5'::jsonb)
                WHERE id = (
                    SELECT id FROM source_record
                    WHERE batch_id = :batch_id
                    ORDER BY id LIMIT 1
                )
                """
            ),
            {"batch_id": batch_id},
        )

    response = client.post(f"/api/v1/snapshots/{snapshot_id}/publish")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "SNAPSHOT_QUALITY_FAILED"
    assert any(
        issue["error_code"] == "NON_CANONICAL_JSON"
        and issue["field"] == "source_record.lineage"
        for issue in response.json()["detail"]["issues"]
    )
    with engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT status, published_at FROM accounting_snapshot WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).one()
    assert state == ("DRAFT", None)


@pytest.mark.parametrize("json_value", ["null", "[]", '"text"', "7"])
def test_publish_nonobject_snapshot_lineage_is_422_and_leaves_draft(
    api_resources: tuple[TestClient, Engine],
    json_value: str,
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)
    validated = _validate(client, company_code, batch_id)
    assert validated.status_code == 200 and validated.json()["valid"] is True
    snapshot_id = validated.json()["snapshot"]["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE accounting_snapshot
                SET lineage = CAST(:json_value AS jsonb)
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id, "json_value": json_value},
        )

    response = client.post(f"/api/v1/snapshots/{snapshot_id}/publish")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "SNAPSHOT_QUALITY_FAILED"
    assert any(
        issue["error_code"] == "NON_CANONICAL_JSON"
        and issue["field"] == "accounting_snapshot.lineage"
        for issue in response.json()["detail"]["issues"]
    )
    with engine.connect() as connection:
        state = connection.execute(
            text(
                "SELECT status, published_at FROM accounting_snapshot WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).one()
    assert state == ("DRAFT", None)


@pytest.mark.parametrize("json_value", ["null", "[]", '"text"', "7"])
def test_revalidate_nonobject_draft_lineage_is_explicit_data_quality(
    api_resources: tuple[TestClient, Engine],
    json_value: str,
) -> None:
    client, engine = api_resources
    company_code, batch_id = _seed_api_case(engine)
    validated = _validate(client, company_code, batch_id)
    assert validated.status_code == 200 and validated.json()["valid"] is True
    snapshot_id = validated.json()["snapshot"]["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE accounting_snapshot
                SET lineage = CAST(:json_value AS jsonb)
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id, "json_value": json_value},
        )

    response = _validate(client, company_code, batch_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["snapshot"] is None
    assert any(
        issue["error_code"] == "NON_CANONICAL_JSON"
        and issue["field"] == "accounting_snapshot.lineage"
        for issue in body["issues"]
    )
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT status, published_at FROM accounting_snapshot
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).all()
    assert rows == [("DRAFT", None)]


@pytest.mark.parametrize(
    ("payload_factory", "expected_code"),
    [
        (
            lambda: {
                "company_code": "X",
                "period": "2026-06-29",
                "source_batch_ids": [str(uuid4())],
            },
            "INVALID_QUARTER_PERIOD",
        ),
        (
            lambda: (
                lambda batch_id: {
                    "company_code": "X",
                    "period": PERIOD.isoformat(),
                    "source_batch_ids": [str(batch_id), str(batch_id)],
                }
            )(uuid4()),
            "DUPLICATE_SOURCE_BATCH",
        ),
        (
            lambda: {
                "company_code": "X",
                "period": PERIOD.isoformat(),
                "source_batch_ids": [str(uuid4())],
                "accepted_partial_batch_ids": [str(uuid4())],
            },
            "PARTIAL_BATCH_NOT_SELECTED",
        ),
        (
            lambda: {
                "company_code": "X",
                "period": PERIOD.isoformat(),
                "source_batch_ids": [],
            },
            "SOURCE_BATCH_REQUIRED",
        ),
    ],
)
def test_validate_api_preserves_stable_business_codes_before_quality_gate(
    api_resources: tuple[TestClient, Engine],
    payload_factory: object,
    expected_code: str,
) -> None:
    client, _ = api_resources
    assert callable(payload_factory)

    response = client.post("/api/v1/snapshots/validate", json=payload_factory())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code


def test_snapshot_api_rejects_oversized_collections_before_database_queries(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        source_response = client.post(
            "/api/v1/snapshots/validate",
            json={
                "company_code": "LIMIT-COMPANY",
                "period": PERIOD.isoformat(),
                "source_batch_ids": [
                    str(uuid4()) for _ in range(MAX_SNAPSHOT_SOURCE_BATCHES + 1)
                ],
            },
        )
        set_response = client.post(
            "/api/v1/snapshot-sets",
            json={
                "set_key": "limit-set",
                "period": PERIOD.isoformat(),
                "expected_members": [
                    {"company_id": str(uuid4()), "snapshot_id": str(uuid4())}
                    for _ in range(MAX_SNAPSHOT_SET_MEMBERS + 1)
                ],
            },
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert source_response.status_code == 422
    assert set_response.status_code == 422
    assert statements == []


def test_publish_missing_or_quality_drift_has_stable_http_errors(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    missing = client.post(f"/api/v1/snapshots/{uuid4()}/publish")
    company_code, batch_id = _seed_api_case(engine)
    validated = _validate(client, company_code, batch_id).json()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ingest_batch SET control_total = 99 WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    drift = client.post(
        f"/api/v1/snapshots/{validated['snapshot']['id']}/publish"
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "SNAPSHOT_NOT_FOUND"
    assert drift.status_code == 422
    assert drift.json()["detail"]["code"] == "SNAPSHOT_QUALITY_FAILED"
    assert drift.json()["detail"]["issues"][0]["category"] == "DATA_QUALITY"


@pytest.fixture(scope="module")
def set_api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine, tuple[dict[str, str], ...]]]:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
    )
    with TestClient(app) as client:
        members: list[dict[str, str]] = []
        for _ in range(101):
            company_code, batch_id = _seed_api_case(engine)
            validated = _validate(client, company_code, batch_id)
            assert validated.status_code == 200, validated.text
            snapshot = validated.json()["snapshot"]
            published = client.post(
                f"/api/v1/snapshots/{snapshot['id']}/publish"
            )
            assert published.status_code == 200, published.text
            members.append(
                {
                    "company_id": snapshot["company_id"],
                    "snapshot_id": snapshot["id"],
                }
            )
        yield client, engine, tuple(members)
    engine.dispose()


def test_snapshot_set_api_rejects_99_and_publishes_100_or_101_with_utc_z(
    set_api_resources: tuple[TestClient, Engine, tuple[dict[str, str], ...]],
) -> None:
    client, _, members = set_api_resources
    too_small = client.post(
        "/api/v1/snapshot-sets",
        json={
            "set_key": f"api-set-99-{uuid4().hex}",
            "period": PERIOD.isoformat(),
            "expected_members": members[:99],
        },
    )

    assert too_small.status_code == 422
    assert too_small.json()["detail"]["code"] == "SNAPSHOT_SET_TOO_SMALL"
    for member_count in (100, 101):
        response = client.post(
            "/api/v1/snapshot-sets",
            json={
                "set_key": f"api-set-{member_count}-{uuid4().hex}",
                "period": PERIOD.isoformat(),
                "expected_members": members[:member_count],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "PUBLISHED"
        assert body["expected_member_count"] == member_count
        assert len(body["members"]) == member_count
        assert re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z",
            body["published_at"],
        )
        assert set(body) == {
            "id",
            "set_key",
            "period",
            "status",
            "expected_member_count",
            "published_at",
            "supersedes_snapshot_set_id",
            "members",
        }
        assert all(set(member) == {"company_id", "snapshot_id"} for member in body["members"])


def test_snapshot_set_api_identity_substitution_is_stable_422_without_rows(
    set_api_resources: tuple[TestClient, Engine, tuple[dict[str, str], ...]],
) -> None:
    client, engine, members = set_api_resources
    set_key = f"api-identity-{uuid4().hex}"
    substituted = [dict(member) for member in members[:100]]
    substituted[0]["company_id"], substituted[1]["company_id"] = (
        substituted[1]["company_id"],
        substituted[0]["company_id"],
    )

    response = client.post(
        "/api/v1/snapshot-sets",
        json={
            "set_key": set_key,
            "period": PERIOD.isoformat(),
            "expected_members": substituted,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SNAPSHOT_SET_IDENTITY_MISMATCH"
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM snapshot_set WHERE set_key = :set_key"),
            {"set_key": set_key},
        ).scalar_one() == 0


@pytest.mark.parametrize(
    ("duplicate_field", "expected_code"),
    [
        ("company_id", "DUPLICATE_SNAPSHOT_SET_COMPANY"),
        ("snapshot_id", "DUPLICATE_SNAPSHOT_SET_SNAPSHOT"),
    ],
)
def test_snapshot_set_api_preserves_stable_duplicate_member_codes(
    set_api_resources: tuple[TestClient, Engine, tuple[dict[str, str], ...]],
    duplicate_field: str,
    expected_code: str,
) -> None:
    client, _, members = set_api_resources
    duplicated = [dict(member) for member in members[:100]]
    duplicated[1][duplicate_field] = duplicated[0][duplicate_field]

    response = client.post(
        "/api/v1/snapshot-sets",
        json={
            "set_key": f"api-duplicate-{uuid4().hex}",
            "period": PERIOD.isoformat(),
            "expected_members": duplicated,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code

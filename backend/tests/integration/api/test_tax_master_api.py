from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from functools import partial
from hashlib import sha256
import hmac
from io import BytesIO
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import Engine, text
from starlette.requests import Request

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import Principal


HEADERS = (
    "company_code",
    "company_name",
    "valid_from",
    "valid_to",
    "tax_rate",
    "deferred_tax_rate",
    "loss_carryforward",
    "three_year_average_tax_burden",
)
DEV_SECRET = "tax-master-api-development-secret"
MAKER_SUBJECT = "group-tax-maker@example.com"
REVIEWER_SUBJECT = "group-tax-reviewer@example.com"


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


def _group_tax_maker(_request: Request) -> Principal:
    return Principal(
        subject=MAKER_SUBJECT,
        roles=frozenset({"group-tax"}),
        allowed_company_ids=frozenset(),
        organization_path="/GROUP/TAX",
    )


def _xlsx(
    company_code: str,
    company_name: str,
    *,
    valid_from: date = date(2026, 1, 1),
    valid_to: date | None = None,
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "tax_master"
    worksheet.append(HEADERS)
    worksheet.append(
        (
            company_code,
            company_name,
            valid_from,
            valid_to,
            "25%",
            "20%",
            "123.45",
            "9%",
        )
    )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


@pytest.fixture
def api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(
            environment="development",
            development_principal_enabled=True,
            development_principal_secret=DEV_SECRET,
        ),
    )
    with TestClient(app) as client:
        client.headers.update(_principal_headers(MAKER_SUBJECT))
        yield client, engine
    engine.dispose()


def _seed_company(engine: Engine, company_code: str, company_name: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:company_code, :company_name)
                """
            ),
            {"company_code": company_code, "company_name": company_name},
        )


def _import(
    client: TestClient,
    payload: bytes,
    *,
    filename: str = "tax-master.xlsx",
    uploaded_by: str = "maker@example.com",
    currency: str = "CNY",
    amount_scale: str = "2",
):
    return client.post(
        "/api/v1/tax-master/import",
        data={
            "uploaded_by": uploaded_by,
            "currency": currency,
            "amount_scale": amount_scale,
        },
        files={
            "file": (
                filename,
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def test_import_approve_and_quarter_lookup_contract(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code = f"API-TM-{uuid4().hex}"
    _seed_company(engine, company_code, "API Tax Master")
    payload = _xlsx(company_code, "API Tax Master")

    imported = _import(client, payload)
    replay = _import(client, payload)
    renamed = _import(client, payload, filename="renamed-tax-master.xlsx")

    assert imported.status_code == 201, imported.text
    assert replay.status_code == 200, replay.text
    assert renamed.status_code == 201, renamed.text
    import_body = imported.json()
    assert replay.json()["batch_id"] == import_body["batch_id"]
    assert replay.json()["version_ids"] == import_body["version_ids"]
    assert renamed.json()["batch_id"] != import_body["batch_id"]
    assert renamed.json()["version_ids"] != import_body["version_ids"]
    assert import_body["source_filename"] == "tax-master.xlsx"
    assert import_body["uploaded_by"] == MAKER_SUBJECT
    assert import_body["currency"] == "CNY"
    assert import_body["amount_scale"] == 2
    assert import_body["replayed"] is False

    version_id = import_body["version_ids"][0]
    same_person = client.post(
        f"/api/v1/tax-master/{version_id}/approve",
        json={"reviewed_by": "untrusted-same-person@example.com"},
    )
    approved = client.post(
        f"/api/v1/tax-master/{version_id}/approve",
        headers=_principal_headers(REVIEWER_SUBJECT),
        json={"reviewed_by": "untrusted-reviewer@example.com"},
    )
    resolved = client.get(f"/api/v1/tax-master/{company_code}?period=2026-Q2")

    assert same_person.status_code == 409
    assert same_person.json()["detail"]["code"] == "MAKER_REVIEWER_CONFLICT"
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "PUBLISHED"
    assert approved.json()["approved_by"] == REVIEWER_SUBJECT
    assert approved.json()["published_at"].endswith("Z")
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["id"] == version_id
    assert resolved.json()["company_code"] == company_code
    assert resolved.json()["tax_rate"] == "0.250000000000"
    assert resolved.json()["deferred_tax_rate"] == "0.200000000000"
    assert resolved.json()["three_year_average_tax_burden"] == "0.090000000000"


@pytest.mark.parametrize("period", ["2026-Q0", "2026-Q5", "2026-Q1x", "26-Q1", "2026-1"])
def test_lookup_rejects_noncanonical_quarter_period_with_stable_code(
    api_resources: tuple[TestClient, Engine],
    period: str,
) -> None:
    client, _ = api_resources

    response = client.get(f"/api/v1/tax-master/DOES-NOT-MATTER?period={period}")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_QUARTER_PERIOD"


def test_missing_version_and_missing_effective_master_return_stable_404(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code = f"API-MISSING-{uuid4().hex}"
    _seed_company(engine, company_code, "No Master")

    missing_approval = client.post(
        f"/api/v1/tax-master/{uuid4()}/approve",
        json={"reviewed_by": "reviewer@example.com"},
    )
    missing_lookup = client.get(f"/api/v1/tax-master/{company_code}?period=2026-Q1")

    assert missing_approval.status_code == 404
    assert missing_approval.json()["detail"]["code"] == "TAX_MASTER_VERSION_NOT_FOUND"
    assert missing_lookup.status_code == 404
    assert missing_lookup.json()["detail"]["code"] == "TAX_MASTER_NOT_FOUND"


def test_invalid_workbook_and_import_options_return_stable_422_without_partial_versions(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, engine = api_resources
    company_code = f"API-INVALID-{uuid4().hex}"
    _seed_company(engine, company_code, "Invalid Workbook")
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(HEADERS + ("extra",))
    worksheet.append(
        (
            company_code,
            "Invalid Workbook",
            date(2026, 1, 1),
            None,
            "25%",
            "20%",
            "0.00",
            "9%",
            "unexpected",
        )
    )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()

    invalid_workbook = _import(client, stream.getvalue())
    invalid_workbook_replay = _import(client, stream.getvalue())
    invalid_currency = _import(client, _xlsx(company_code, "Invalid Workbook"), currency="CN")
    invalid_scale = _import(
        client,
        _xlsx(company_code, "Invalid Workbook"),
        amount_scale="13",
    )

    assert invalid_workbook.status_code == 422
    assert invalid_workbook.json()["detail"]["code"] == "INVALID_HEADER"
    assert invalid_workbook_replay.status_code == 422
    assert invalid_workbook_replay.json()["detail"] == invalid_workbook.json()["detail"]
    assert invalid_workbook.json()["detail"]["batch_id"] is not None
    assert invalid_currency.status_code == 422
    assert invalid_currency.json()["detail"]["code"] == "INVALID_IMPORT_OPTIONS"
    assert invalid_scale.status_code == 422
    assert invalid_scale.json()["detail"]["code"] == "INVALID_IMPORT_OPTIONS"
    with engine.connect() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM tax_master_version AS version
                JOIN company ON company.id = version.company_id
                WHERE company.company_code = :company_code
                """
            ),
            {"company_code": company_code},
        ).scalar_one()
        failed_batch_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM ingest_batch
                WHERE id = :batch_id AND status = 'FAILED'
                """
            ),
            {"batch_id": invalid_workbook.json()["detail"]["batch_id"]},
        ).scalar_one()
        failed_error_count = connection.execute(
            text("SELECT count(*) FROM ingest_error WHERE batch_id = :batch_id"),
            {"batch_id": invalid_workbook.json()["detail"]["batch_id"]},
        ).scalar_one()
    assert count == 0
    assert failed_batch_count == 1
    assert failed_error_count == 1


def test_tax_master_upload_reuses_global_bounded_read_limit(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(ingest_max_upload_bytes=16),
        principal_provider=_group_tax_maker,
    )
    with TestClient(app) as client:
        response = _import(client, b"x" * 17)
    engine.dispose()

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"


def test_xlsx_resource_limit_is_stable_and_failed_audited_through_api(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=Settings(tax_master_xlsx_max_zip_members=1),
        principal_provider=_group_tax_maker,
    )
    with TestClient(app) as client:
        response = _import(client, _xlsx("LIMITED", "Limited Company"))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "XLSX_RESOURCE_LIMIT_EXCEEDED"
    with engine.connect() as connection:
        audited = connection.execute(
            text(
                """
                SELECT batch.status, error.error_code
                FROM ingest_batch AS batch
                JOIN ingest_error AS error ON error.batch_id = batch.id
                WHERE batch.id = :batch_id
                """
            ),
            {"batch_id": detail["batch_id"]},
        ).one()
    engine.dispose()

    assert audited == ("FAILED", "XLSX_RESOURCE_LIMIT_EXCEEDED")

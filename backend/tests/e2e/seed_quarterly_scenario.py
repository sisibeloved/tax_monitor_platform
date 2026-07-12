from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from io import BytesIO, StringIO
import re
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from httpx import Response
from openpyxl import Workbook  # type: ignore[import-untyped]
from sqlalchemy import Engine
from sqlalchemy import text


PERIOD = date(2026, 6, 30)
METRICS = (
    ("cumulative_profit", "10000000"),
    ("received_dividends", "1000000"),
    ("fair_value_change", "500000"),
    ("cumulative_revenue", "50000000"),
    ("prior_quarter_current_tax", "900000"),
    ("current_quarter_current_tax", "700000"),
    ("other_payables_accrual", "1400000"),
    ("hesi_no_invoice", "300000"),
)
TAX_MASTER_HEADERS = (
    "company_code",
    "company_name",
    "valid_from",
    "valid_to",
    "tax_rate",
    "loss_carryforward",
    "three_year_average_tax_burden",
)
SEED_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,31}")


class ScenarioClient(Protocol):
    """Small HTTP seam shared by TestClient and an external httpx client."""

    def post(self, url: str, **kwargs: Any) -> Response: ...


@dataclass(frozen=True, slots=True)
class QuarterlyScenarioSeed:
    snapshot_set_id: UUID
    rule_version_id: UUID
    company_ids: tuple[UUID, ...]
    company_codes: tuple[str, ...]
    snapshot_ids: tuple[UUID, ...]
    tax_master_version_ids: tuple[UUID, ...]
    sap_batch_id: UUID
    standard_company_id: UUID
    standard_company_code: str
    standard_snapshot_id: UUID
    ineffective_master_company_id: UUID | None
    inactive_company_id: UUID | None


def seed_quarterly_scenario(
    client: ScenarioClient,
    engine: Engine,
    *,
    company_count: int,
    inject_blockers: bool = False,
    token: str | None = None,
) -> QuarterlyScenarioSeed:
    if company_count < 100:
        raise ValueError("published snapshot sets require at least 100 companies")
    if inject_blockers and company_count < 3:
        raise ValueError("blocker isolation requires at least three companies")

    token = uuid4().hex if token is None else token
    if SEED_TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError(
            "seed token must be 6-32 safe characters: letters, digits, underscore, or hyphen"
        )
    company_codes = tuple(f"E2E-{token}-{index:03d}" for index in range(company_count))
    company_names = tuple(f"E2E Quarterly Company {index:03d}" for index in range(company_count))

    company_batch_id = _create_batch(
        client,
        token=token,
        suffix="company-master",
        source="COMPANY_REGISTRY",
        dataset_code="company_master",
    )
    company_rows = (
        (
            f"company-{index:03d}",
            company_code,
            company_name,
            "ACTIVE",
            "2026-07-01T08:00:00+00:00",
        )
        for index, (company_code, company_name) in enumerate(
            zip(company_codes, company_names, strict=True)
        )
    )
    _upload_csv(
        client,
        company_batch_id,
        _csv_bytes(
            (
                "source_record_key",
                "company_code",
                "company_name",
                "lifecycle",
                "extracted_at",
            ),
            company_rows,
        ),
    )

    master_import = client.post(
        "/api/v1/tax-master/import",
        data={
            "uploaded_by": "e2e-maker@example.com",
            "currency": "CNY",
            "amount_scale": "2",
        },
        files={
            "file": (
                f"quarterly-master-{token}.xlsx",
                _tax_master_workbook(company_codes, company_names),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    _assert_status(master_import, 201, "import tax master")
    version_by_company: dict[str, UUID] = {}
    for raw_version_id in master_import.json()["version_ids"]:
        approved = client.post(
            f"/api/v1/tax-master/{raw_version_id}/approve",
            json={"reviewed_by": "e2e-reviewer@example.com"},
        )
        _assert_status(approved, 200, "approve tax master")
        version_by_company[approved.json()["company_code"]] = UUID(raw_version_id)
    tax_master_version_ids = tuple(version_by_company[code] for code in company_codes)

    sap_batch_id = _create_batch(
        client,
        token=token,
        suffix="sap-quarterly",
        source="SAP",
        dataset_code="quarterly_metric",
    )
    financial_rows = (
        (
            f"sap-{company_index:03d}-{metric_index:02d}",
            company_code,
            "2026",
            PERIOD.isoformat(),
            "CNY",
            "2",
            metric_code,
            amount,
            "2026-07-01T08:05:00+00:00",
        )
        for company_index, company_code in enumerate(company_codes)
        for metric_index, (metric_code, amount) in enumerate(METRICS)
    )
    uploaded_financials = _upload_csv(
        client,
        sap_batch_id,
        _csv_bytes(
            (
                "source_record_key",
                "company_code",
                "fiscal_year",
                "period",
                "currency",
                "amount_scale",
                "metric_code",
                "amount",
                "extracted_at",
            ),
            financial_rows,
        ),
    )
    assert uploaded_financials["accepted_count"] == company_count * len(METRICS)
    assert uploaded_financials["rejected_count"] == 0

    company_ids: list[UUID] = []
    snapshot_ids: list[UUID] = []
    for company_code in company_codes:
        validated = client.post(
            "/api/v1/snapshots/validate",
            json={
                "company_code": company_code,
                "period": PERIOD.isoformat(),
                "source_batch_ids": [str(sap_batch_id)],
                "accepted_partial_batch_ids": [],
            },
        )
        _assert_status(validated, 200, "validate accounting snapshot")
        validated_body = validated.json()
        assert validated_body["valid"] is True, validated.text
        snapshot = validated_body["snapshot"]
        published = client.post(f"/api/v1/snapshots/{snapshot['id']}/publish")
        _assert_status(published, 200, "publish accounting snapshot")
        published_body = published.json()
        company_ids.append(UUID(published_body["company_id"]))
        snapshot_ids.append(UUID(published_body["id"]))

    snapshot_set = client.post(
        "/api/v1/snapshot-sets",
        json={
            "set_key": f"e2e-quarterly-set-{token}",
            "period": PERIOD.isoformat(),
            "expected_members": [
                {"company_id": str(company_id), "snapshot_id": str(snapshot_id)}
                for company_id, snapshot_id in zip(
                    company_ids,
                    snapshot_ids,
                    strict=True,
                )
            ],
        },
    )
    _assert_status(snapshot_set, 201, "publish accounting snapshot set")

    with engine.connect() as connection:
        rule_version_id = connection.execute(
            text(
                """
                SELECT id
                FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1'
                  AND version = 'phase-1-reviewed'
                  AND status = 'PUBLISHED'
                """
            )
        ).scalar_one()

    ineffective_master_company_id: UUID | None = None
    inactive_company_id: UUID | None = None
    if inject_blockers:
        ineffective_master_company_id = company_ids[-2]
        inactive_company_id = company_ids[-1]
        # There is deliberately no public endpoint that can unpublish a frozen
        # master. This one state-drift injection simulates the source/master
        # disappearing after snapshot publication; all outcomes remain real.
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE tax_master_version
                    SET status = 'DRAFT', published_at = NULL, approved_by = NULL
                    WHERE id = :version_id
                    """
                ),
                {"version_id": tax_master_version_ids[-2]},
            )

        inactive_batch_id = _create_batch(
            client,
            token=token,
            suffix="company-inactive",
            source="COMPANY_REGISTRY",
            dataset_code="company_master",
        )
        _upload_csv(
            client,
            inactive_batch_id,
            _csv_bytes(
                (
                    "source_record_key",
                    "company_code",
                    "company_name",
                    "lifecycle",
                    "extracted_at",
                ),
                (
                    (
                        "company-source-anomaly",
                        company_codes[-1],
                        company_names[-1],
                        "INACTIVE",
                        "2026-07-02T08:00:00+00:00",
                    ),
                ),
            ),
        )

    return QuarterlyScenarioSeed(
        snapshot_set_id=UUID(snapshot_set.json()["id"]),
        rule_version_id=rule_version_id,
        company_ids=tuple(company_ids),
        company_codes=company_codes,
        snapshot_ids=tuple(snapshot_ids),
        tax_master_version_ids=tax_master_version_ids,
        sap_batch_id=sap_batch_id,
        standard_company_id=company_ids[0],
        standard_company_code=company_codes[0],
        standard_snapshot_id=snapshot_ids[0],
        ineffective_master_company_id=ineffective_master_company_id,
        inactive_company_id=inactive_company_id,
    )


def _create_batch(
    client: ScenarioClient,
    *,
    token: str,
    suffix: str,
    source: str,
    dataset_code: str,
) -> UUID:
    response = client.post(
        "/api/v1/ingest-batches",
        json={
            "source": source,
            "source_batch_key": f"e2e-{token}-{suffix}",
            "dataset_code": dataset_code,
            "extraction_time": "2026-07-03T08:00:00Z",
            "period": PERIOD.isoformat(),
            "mode": "FULL",
            "schema_version": "e2e-v1",
            "currency": "CNY",
            "amount_scale": 2,
            "source_primary_key_definition": {"fields": ["source_record_key"]},
        },
    )
    _assert_status(response, 201, f"create {suffix} ingest batch")
    return UUID(response.json()["id"])


def _upload_csv(
    client: ScenarioClient,
    batch_id: UUID,
    payload: bytes,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/ingest-batches/{batch_id}/files",
        files={"file": ("source.csv", payload, "text/csv")},
    )
    _assert_status(response, 200, "upload controlled CSV")
    body = cast(dict[str, object], response.json())
    assert body["status"] == "SUCCEEDED", response.text
    return body


def _csv_bytes(
    headers: tuple[str, ...],
    rows: Iterable[Sequence[object]],
) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode()


def _tax_master_workbook(
    company_codes: tuple[str, ...],
    company_names: tuple[str, ...],
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "tax_master"
    worksheet.append(TAX_MASTER_HEADERS)
    for company_code, company_name in zip(company_codes, company_names, strict=True):
        worksheet.append(
            (
                company_code,
                company_name,
                date(2026, 1, 1),
                None,
                "25%",
                "2000000",
                "9%",
            )
        )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _assert_status(response: Response, expected: int, operation: str) -> None:
    if response.status_code != expected:
        raise AssertionError(
            f"{operation} returned {response.status_code}, expected {expected}: "
            f"{response.text}"
        )

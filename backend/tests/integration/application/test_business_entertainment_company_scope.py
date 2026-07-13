from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from functools import partial
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tax_risk.application.business_entertainment.company_scope import (
    BusinessEntertainmentScopeService,
    ScopeConflictError,
    ScopeNotReadyError,
    ScopeValidationError,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


HEADERS = ("company_code", "effective_from", "effective_to")


def _xlsx(rows: list[tuple[object, object, object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "business_entertainment_scope"
    worksheet.append(HEADERS)
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _seed_company(
    engine: Engine,
    company_code: str,
    *,
    lifecycle: str = "ACTIVE",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company (
                    company_code, company_name, lifecycle, deactivated_at
                )
                VALUES (
                    :company_code, :company_name,
                    CAST(:lifecycle AS company_lifecycle),
                    CASE WHEN CAST(:lifecycle AS text) = 'INACTIVE' THEN now() ELSE NULL END
                )
                """
            ),
            {
                "company_code": company_code,
                "company_name": f"Scope Company {company_code}",
                "lifecycle": lifecycle,
            },
        )


@pytest.fixture
def scope_resources(
    isolated_database_url: str,
) -> Iterator[tuple[BusinessEntertainmentScopeService, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        yield BusinessEntertainmentScopeService(partial(UnitOfWork, factory)), engine
    finally:
        engine.dispose()


def test_import_approve_publish_and_resolve_scope_with_ingest_lineage(
    scope_resources: tuple[BusinessEntertainmentScopeService, Engine],
) -> None:
    service, engine = scope_resources
    token = uuid4().hex
    company_codes = (f"BE-{token}-1", f"BE-{token}-2")
    for company_code in company_codes:
        _seed_company(engine, company_code)

    payload = _xlsx(
        [
            (company_codes[0], "2026-01-01", "2026-12-31"),
            (company_codes[1], "2026-01-01", "2026-12-31"),
        ]
    )
    imported = service.import_xlsx(
        filename="business-entertainment-scope.xlsx",
        payload=payload,
        uploaded_by="scope-maker@example.com",
    )
    replayed = service.import_xlsx(
        filename="business-entertainment-scope.xlsx",
        payload=payload,
        uploaded_by="scope-maker@example.com",
    )

    assert replayed.version_id == imported.version_id
    assert replayed.batch_id == imported.batch_id

    with pytest.raises(ScopeConflictError, match="reviewer must be different"):
        service.approve(imported.version_id, reviewed_by="scope-maker@example.com")

    approved = service.approve(
        imported.version_id,
        reviewed_by="scope-reviewer@example.com",
    )
    published = service.publish(
        imported.version_id,
        published_by="scope-reviewer@example.com",
    )
    resolved = service.resolve(effective_on=date(2026, 6, 30))

    assert imported.status.value == "DRAFT"
    assert approved.status.value == "APPROVED"
    assert published.status.value == "PUBLISHED"
    assert published.published_at is not None
    assert published.reviewer_id == "scope-reviewer@example.com"
    assert resolved.version_id == imported.version_id
    assert resolved.company_codes == tuple(sorted(company_codes))

    with engine.connect() as connection:
        batch = connection.execute(
            text(
                """
                SELECT source, dataset_code, status, record_count,
                       accepted_count, rejected_count, checksum
                FROM ingest_batch
                WHERE id = :batch_id
                """
            ),
            {"batch_id": imported.batch_id},
        ).mappings().one()
        source_record_count = connection.execute(
            text("SELECT count(*) FROM source_record WHERE batch_id = :batch_id"),
            {"batch_id": imported.batch_id},
        ).scalar_one()
        scope_row_count = connection.execute(
            text(
                "SELECT count(*) FROM business_entertainment_scope_company "
                "WHERE version_id = :version_id"
            ),
            {"version_id": imported.version_id},
        ).scalar_one()

    assert batch["source"] == "BUSINESS_ENTERTAINMENT_SCOPE_XLSX"
    assert batch["dataset_code"] == "business_entertainment_company_scope"
    assert batch["status"] == "SUCCEEDED"
    assert (batch["record_count"], batch["accepted_count"], batch["rejected_count"]) == (
        2,
        2,
        0,
    )
    assert len(batch["checksum"]) == 64
    assert source_record_count == 2
    assert scope_row_count == 2


def test_unknown_or_inactive_company_blocks_the_whole_import(
    scope_resources: tuple[BusinessEntertainmentScopeService, Engine],
) -> None:
    service, engine = scope_resources
    inactive_code = f"BE-INACTIVE-{uuid4().hex}"
    unknown_code = f"BE-UNKNOWN-{uuid4().hex}"
    _seed_company(engine, inactive_code, lifecycle="INACTIVE")
    payload = _xlsx(
        [
            (inactive_code, "2026-01-01", "2026-12-31"),
            (unknown_code, "2026-01-01", "2026-12-31"),
        ]
    )

    with pytest.raises(ScopeValidationError) as captured:
        service.import_xlsx(
            filename="invalid-scope.xlsx",
            payload=payload,
            uploaded_by="scope-maker@example.com",
        )
    with pytest.raises(ScopeValidationError) as replayed:
        service.import_xlsx(
            filename="invalid-scope.xlsx",
            payload=payload,
            uploaded_by="scope-maker@example.com",
        )

    assert {issue.error_code for issue in captured.value.issues} == {
        "INACTIVE_COMPANY",
        "UNKNOWN_COMPANY",
    }
    assert replayed.value.batch_id == captured.value.batch_id
    assert replayed.value.issues == captured.value.issues


def test_unpublished_or_missing_effective_scope_is_blocking(
    scope_resources: tuple[BusinessEntertainmentScopeService, Engine],
) -> None:
    service, engine = scope_resources
    company_code = f"BE-DRAFT-{uuid4().hex}"
    _seed_company(engine, company_code)
    service.import_xlsx(
        filename="draft-scope.xlsx",
        payload=_xlsx([(company_code, "2027-01-01", "2027-12-31")]),
        uploaded_by="scope-maker@example.com",
    )

    with pytest.raises(ScopeNotReadyError) as captured:
        service.resolve(effective_on=date(2027, 6, 30))

    assert captured.value.error_code == "BUSINESS_ENTERTAINMENT_SCOPE_NOT_READY"


def test_overlapping_published_scope_version_is_rejected(
    scope_resources: tuple[BusinessEntertainmentScopeService, Engine],
) -> None:
    service, engine = scope_resources
    company_code = f"BE-OVERLAP-{uuid4().hex}"
    _seed_company(engine, company_code)

    first = service.import_xlsx(
        filename="scope-h1.xlsx",
        payload=_xlsx([(company_code, "2028-01-01", "2028-06-30")]),
        uploaded_by="maker-1@example.com",
    )
    service.approve(first.version_id, reviewed_by="reviewer-1@example.com")
    service.publish(first.version_id, published_by="reviewer-1@example.com")

    second = service.import_xlsx(
        filename="scope-overlap.xlsx",
        payload=_xlsx([(company_code, "2028-06-01", "2028-12-31")]),
        uploaded_by="maker-2@example.com",
    )
    service.approve(second.version_id, reviewed_by="reviewer-2@example.com")

    with pytest.raises(ScopeConflictError) as captured:
        service.publish(second.version_id, published_by="reviewer-2@example.com")

    assert captured.value.error_code == "BUSINESS_ENTERTAINMENT_SCOPE_PERIOD_OVERLAP"


def test_published_scope_version_and_company_rows_are_immutable(
    scope_resources: tuple[BusinessEntertainmentScopeService, Engine],
) -> None:
    service, engine = scope_resources
    company_code = f"BE-IMMUTABLE-{uuid4().hex}"
    _seed_company(engine, company_code)
    imported = service.import_xlsx(
        filename="scope-immutable.xlsx",
        payload=_xlsx([(company_code, "2029-01-01", "2029-12-31")]),
        uploaded_by="scope-maker@example.com",
    )
    service.approve(imported.version_id, reviewed_by="scope-reviewer@example.com")
    service.publish(imported.version_id, published_by="scope-reviewer@example.com")

    with pytest.raises(DBAPIError, match="immutable_business_entertainment_scope"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE business_entertainment_scope_version "
                    "SET source_file_name = 'changed.xlsx' WHERE id = :version_id"
                ),
                {"version_id": imported.version_id},
            )

    with pytest.raises(DBAPIError, match="immutable_business_entertainment_scope"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM business_entertainment_scope_company "
                    "WHERE version_id = :version_id"
                ),
                {"version_id": imported.version_id},
            )

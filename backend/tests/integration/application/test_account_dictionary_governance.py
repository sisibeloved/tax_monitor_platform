from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from functools import partial
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tax_risk.application.semantic.account_dictionary import (
    AccountDictionaryConflictError,
    AccountDictionaryNotReadyError,
    SuggestedAccountDictionaryService,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


HEADERS = (
    "dictionary_version",
    "account_id",
    "account_code",
    "account_name",
    "accounting_classification",
    "allowed_monitor_types",
    "allowed_labels",
    "effective_from",
    "effective_to",
    "status",
)


def _xlsx(version: str = "accounts-v1", *, year: int = 2026) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "suggested_accounts"
    worksheet.append(HEADERS)
    rows = (
        ("MEETING_EXPENSE", "660201", "会议费", "PERIOD_EXPENSE", "BUSINESS_ENTERTAINMENT", "MEETING_EXPENSE"),
        ("EMPLOYEE_EDUCATION", "660202", "职工教育经费", "EMPLOYEE_COMPENSATION", "BUSINESS_ENTERTAINMENT", "EMPLOYEE_EDUCATION"),
        ("EMPLOYEE_WELFARE", "660203", "职工福利费", "EMPLOYEE_COMPENSATION", "BUSINESS_ENTERTAINMENT", "EMPLOYEE_WELFARE"),
        ("MANUAL_REVIEW", "REVIEW", "人工复核", "REVIEW", "BUSINESS_ENTERTAINMENT", "MANUAL_REVIEW,INSUFFICIENT_EVIDENCE"),
    )
    for account_id, code, name, classification, monitors, labels in rows:
        worksheet.append(
            (
                version,
                account_id,
                code,
                name,
                classification,
                monitors,
                labels,
                f"{year}-01-01",
                f"{year}-12-31",
                "ACTIVE",
            )
        )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


@pytest.fixture
def account_resources(
    isolated_database_url: str,
) -> Iterator[tuple[SuggestedAccountDictionaryService, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        yield SuggestedAccountDictionaryService(partial(UnitOfWork, factory)), engine
    finally:
        engine.dispose()


def test_import_approve_publish_and_resolve_only_compatible_accounts(
    account_resources: tuple[SuggestedAccountDictionaryService, Engine],
) -> None:
    service, engine = account_resources
    imported = service.import_xlsx(
        filename="建议科目字典.xlsx",
        payload=_xlsx(),
        uploaded_by="account-maker@example.com",
    )

    with pytest.raises(AccountDictionaryConflictError, match="different"):
        service.approve(imported.version_id, reviewed_by="account-maker@example.com")

    approved = service.approve(
        imported.version_id,
        reviewed_by="account-reviewer@example.com",
    )
    published = service.publish(
        imported.version_id,
        published_by="account-reviewer@example.com",
    )
    resolved = service.resolve_account(
        dictionary_version="accounts-v1",
        account_id="MEETING_EXPENSE",
        monitor_type="BUSINESS_ENTERTAINMENT",
        semantic_label="MEETING_EXPENSE",
        effective_on=date(2026, 3, 31),
    )

    assert approved.status.value == "APPROVED"
    assert published.status.value == "PUBLISHED"
    assert published.published_at is not None
    assert len(published.checksum) == 64
    assert resolved.account_code == "660201"
    assert resolved.account_name == "会议费"

    with engine.connect() as connection:
        lineage = connection.execute(
            text(
                """
                SELECT b.source, b.dataset_code, b.status, count(sr.id) AS source_count
                FROM ingest_batch AS b
                JOIN source_record AS sr ON sr.batch_id = b.id
                WHERE b.id = :batch_id
                GROUP BY b.source, b.dataset_code, b.status
                """
            ),
            {"batch_id": imported.batch_id},
        ).mappings().one()
    assert lineage == {
        "source": "SUGGESTED_ACCOUNT_DICTIONARY_XLSX",
        "dataset_code": "suggested_account_dictionary",
        "status": "SUCCEEDED",
        "source_count": 4,
    }


def test_unpublished_unknown_or_incompatible_account_is_rejected(
    account_resources: tuple[SuggestedAccountDictionaryService, Engine],
) -> None:
    service, _ = account_resources
    imported = service.import_xlsx(
        filename="draft-accounts.xlsx",
        payload=_xlsx("accounts-draft", year=2027),
        uploaded_by="maker@example.com",
    )
    with pytest.raises(AccountDictionaryNotReadyError, match="published"):
        service.resolve_account(
            dictionary_version="accounts-draft",
            account_id="MEETING_EXPENSE",
            monitor_type="BUSINESS_ENTERTAINMENT",
            semantic_label="MEETING_EXPENSE",
            effective_on=date(2027, 3, 31),
        )

    service.approve(imported.version_id, reviewed_by="reviewer@example.com")
    service.publish(imported.version_id, published_by="reviewer@example.com")
    with pytest.raises(AccountDictionaryNotReadyError, match="not valid"):
        service.resolve_account(
            dictionary_version="accounts-draft",
            account_id="UNKNOWN",
            monitor_type="BUSINESS_ENTERTAINMENT",
            semantic_label="MEETING_EXPENSE",
            effective_on=date(2027, 3, 31),
        )
    with pytest.raises(AccountDictionaryNotReadyError, match="not compatible"):
        service.resolve_account(
            dictionary_version="accounts-draft",
            account_id="MEETING_EXPENSE",
            monitor_type="BUSINESS_ENTERTAINMENT",
            semantic_label="EMPLOYEE_WELFARE",
            effective_on=date(2027, 3, 31),
        )


def test_published_dictionary_and_entries_are_database_immutable(
    account_resources: tuple[SuggestedAccountDictionaryService, Engine],
) -> None:
    service, engine = account_resources
    imported = service.import_xlsx(
        filename="immutable-accounts.xlsx",
        payload=_xlsx("accounts-immutable", year=2028),
        uploaded_by="maker@example.com",
    )
    service.approve(imported.version_id, reviewed_by="reviewer@example.com")
    service.publish(imported.version_id, published_by="reviewer@example.com")

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE suggested_account_entry SET account_name = '篡改' "
                    "WHERE dictionary_version_id = :version_id"
                ),
                {"version_id": imported.version_id},
            )

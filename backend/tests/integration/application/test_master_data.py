from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from functools import partial
from hashlib import sha256
from io import BytesIO
from threading import Barrier, Event
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from tax_risk.application import master_data as master_data_module
from tax_risk.application.master_data import (
    InvalidImportOptionsError,
    MasterDataConflictError,
    MasterDataNotFoundError,
    MasterDataValidationError,
    TaxMasterService,
)
from tax_risk.persistence.ingest_models import IngestBatch
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.persistence.master_models import TaxMasterVersion
from tax_risk.persistence.master_repositories import MasterRepository


HEADERS = (
    "company_code",
    "company_name",
    "valid_from",
    "valid_to",
    "tax_rate",
    "loss_carryforward",
    "three_year_average_tax_burden",
)


def _xlsx(rows: list[tuple[object, ...]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "tax_master"
    worksheet.append(HEADERS)
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _row(
    company_code: str,
    company_name: str,
    *,
    valid_from: date = date(2026, 1, 1),
    valid_to: date | None = None,
    tax_rate: object = "25%",
    loss: object = "100.00",
    burden: object = "9%",
) -> tuple[object, ...]:
    return company_code, company_name, valid_from, valid_to, tax_rate, loss, burden


def _seed_company(
    engine: Engine,
    company_code: str,
    company_name: str,
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
                "company_name": company_name,
                "lifecycle": lifecycle,
            },
        )


@pytest.fixture
def service_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TaxMasterService, Engine]]:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        yield TaxMasterService(partial(UnitOfWork, factory)), engine
    finally:
        engine.dispose()


def test_import_creates_audited_drafts_with_real_ingest_lineage_and_is_idempotent(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-{uuid4().hex}"
    _seed_company(engine, company_code, "Tax Master Company")
    payload = _xlsx([_row(company_code, "Tax Master Company", loss="123.45")])

    imported = service.import_xlsx(
        filename="master.xlsx",
        payload=payload,
        uploaded_by="maker@example.com",
        currency="CNY",
        amount_scale=2,
    )
    replay = service.import_xlsx(
        filename="master.xlsx",
        payload=payload,
        uploaded_by="maker@example.com",
        currency="CNY",
        amount_scale=2,
    )
    renamed = service.import_xlsx(
        filename="renamed-master.xlsx",
        payload=payload,
        uploaded_by="maker@example.com",
        currency="CNY",
        amount_scale=2,
    )

    assert imported.replayed is False
    assert replay.replayed is True
    assert replay.batch_id == imported.batch_id
    assert replay.version_ids == imported.version_ids
    assert renamed.replayed is False
    assert renamed.batch_id != imported.batch_id
    assert renamed.version_ids != imported.version_ids
    assert imported.currency == "CNY"
    assert imported.amount_scale == 2
    with engine.connect() as connection:
        version = connection.execute(
            text(
                """
                SELECT status, uploaded_by, source_row_number, source_file_name,
                       source_checksum, currency, amount_scale, loss_carryforward,
                       created_at
                FROM tax_master_version
                WHERE id = :version_id
                """
            ),
            {"version_id": imported.version_ids[0]},
        ).mappings().one()
        batch = connection.execute(
            text(
                """
                SELECT dataset_code, status, record_count, accepted_count,
                       rejected_count, control_total, checksum, schema_version,
                       source_primary_key_definition
                FROM ingest_batch
                WHERE id = :batch_id
                """
            ),
            {"batch_id": imported.batch_id},
        ).mappings().one()
        fake_source_records = connection.execute(
            text("SELECT count(*) FROM source_record WHERE batch_id = :batch_id"),
            {"batch_id": imported.batch_id},
        ).scalar_one()

    assert version["status"] == "DRAFT"
    assert version["uploaded_by"] == "maker@example.com"
    assert version["source_row_number"] == 2
    assert version["source_file_name"] == "master.xlsx"
    assert version["source_checksum"] == imported.checksum
    assert version["loss_carryforward"] == Decimal("123.450000000000")
    assert version["created_at"].tzinfo is not None
    assert batch["dataset_code"] == "tax_master"
    assert batch["status"] == "SUCCEEDED"
    assert (batch["record_count"], batch["accepted_count"], batch["rejected_count"]) == (1, 1, 0)
    assert batch["control_total"] == Decimal("123.450000000000")
    assert batch["checksum"] == imported.checksum
    assert batch["schema_version"] == "tax-master-xlsx-v1"
    assert batch["source_primary_key_definition"]["uploaded_by"] == "maker@example.com"
    assert fake_source_records == 0


def test_import_flushes_and_refreshes_before_commit_with_no_post_commit_sql(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    _, engine = service_resources
    token = uuid4().hex
    company_code = f"TM-IMPORT-ORDER-{token}"
    filename = f"import-order-{token}.xlsx"
    _seed_company(engine, company_code, "Import Order Company")
    events: list[str] = []

    class RecordingSession(Session):
        def add(self, instance, *, _warn=True) -> None:  # type: ignore[no-untyped-def,override]
            if isinstance(instance, (IngestBatch, TaxMasterVersion)):
                events.append(f"add:{type(instance).__name__}")
            super().add(instance, _warn=_warn)

        def flush(self, objects=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append("flush")
            super().flush(objects)

        def refresh(self, instance, attribute_names=None, with_for_update=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append(f"refresh:{type(instance).__name__}")
            super().refresh(instance, attribute_names, with_for_update)

    factory = sessionmaker(
        bind=engine,
        class_=RecordingSession,
        autoflush=False,
        expire_on_commit=True,
    )

    class RecordingUow(UnitOfWork):
        def commit(self) -> None:
            events.append("commit")
            super().commit()

    def record_sql(*_args: object) -> None:
        events.append("sql")

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        imported = TaxMasterService(partial(RecordingUow, factory)).import_xlsx(
            filename=filename,
            payload=_xlsx([_row(company_code, "Import Order Company")]),
            uploaded_by="maker@example.com",
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    batch_add_index = events.index("add:IngestBatch")
    batch_flush_index = events.index("flush", batch_add_index)
    version_add_index = events.index("add:TaxMasterVersion", batch_flush_index)
    version_flush_index = events.index("flush", version_add_index)
    batch_refresh_index = events.index("refresh:IngestBatch", version_flush_index)
    commit_index = events.index("commit")

    assert imported.source_filename == filename
    assert len(imported.version_ids) == 1
    assert imported.imported_at.tzinfo is not None
    assert (
        batch_add_index
        < batch_flush_index
        < version_add_index
        < version_flush_index
        < batch_refresh_index
        < commit_index
    )
    assert not [
        recorded
        for recorded in events[commit_index + 1 :]
        if recorded == "sql" or recorded.startswith("refresh:")
    ]


def test_import_refresh_failure_rolls_back_before_commit(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    _, engine = service_resources
    token = uuid4().hex
    company_code = f"TM-IMPORT-REFRESH-FAIL-{token}"
    filename = f"import-refresh-failure-{token}.xlsx"
    _seed_company(engine, company_code, "Import Refresh Failure Company")
    events: list[str] = []

    class FailingRefreshSession(Session):
        def refresh(self, instance, attribute_names=None, with_for_update=None) -> None:  # type: ignore[no-untyped-def,override]
            if isinstance(instance, IngestBatch):
                events.append("refresh")
                raise RuntimeError("injected import refresh failure")
            super().refresh(instance, attribute_names, with_for_update)

    factory = sessionmaker(
        bind=engine,
        class_=FailingRefreshSession,
        autoflush=False,
        expire_on_commit=False,
    )

    class RecordingUow(UnitOfWork):
        def commit(self) -> None:
            events.append("commit")
            super().commit()

    with pytest.raises(RuntimeError, match="injected import refresh failure"):
        TaxMasterService(partial(RecordingUow, factory)).import_xlsx(
            filename=filename,
            payload=_xlsx([_row(company_code, "Import Refresh Failure Company")]),
            uploaded_by="maker@example.com",
        )

    assert "refresh" in events
    assert "commit" not in events
    with engine.connect() as connection:
        persisted = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM ingest_batch WHERE payload_ref = :filename),
                    (
                        SELECT count(*)
                        FROM tax_master_version AS version
                        JOIN ingest_batch AS batch ON batch.id = version.source_batch_id
                        WHERE batch.payload_ref = :filename
                    )
                """
            ),
            {"filename": filename},
        ).one()
    assert persisted == (0, 0)


def test_large_loss_entered_as_text_is_persisted_exactly(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-EXACT-{uuid4().hex}"
    _seed_company(engine, company_code, "Exact Decimal Company")
    exact_loss = Decimal("12345678901234567890123456.12")

    imported = service.import_xlsx(
        filename="exact-large-loss.xlsx",
        payload=_xlsx(
            [_row(company_code, "Exact Decimal Company", loss=str(exact_loss))]
        ),
        uploaded_by="maker@example.com",
        amount_scale=2,
    )

    with engine.connect() as connection:
        persisted = connection.execute(
            text(
                """
                SELECT version.loss_carryforward, batch.control_total
                FROM tax_master_version AS version
                JOIN ingest_batch AS batch ON batch.id = version.source_batch_id
                WHERE version.id = :version_id
                """
            ),
            {"version_id": imported.version_ids[0]},
        ).one()
    assert persisted.loss_carryforward == exact_loss
    assert persisted.control_total == exact_loss


@pytest.mark.parametrize(
    ("uploaded_by", "reviewed_by", "normalized"),
    [
        ("  Ｍaker@Example.COM  ", "maker@example.com", "maker@example.com"),
        ("Straße", "STRASSE", "strasse"),
    ],
)
def test_maker_and_reviewer_use_the_same_nfkc_casefold_identity(
    service_resources: tuple[TaxMasterService, Engine],
    uploaded_by: str,
    reviewed_by: str,
    normalized: str,
) -> None:
    service, engine = service_resources
    company_code = f"TM-IDENTITY-{uuid4().hex}"
    _seed_company(engine, company_code, "Identity Company")
    imported = service.import_xlsx(
        filename="identity.xlsx",
        payload=_xlsx([_row(company_code, "Identity Company")]),
        uploaded_by=uploaded_by,
    )

    with pytest.raises(MasterDataConflictError) as caught:
        service.approve(imported.version_ids[0], reviewed_by=reviewed_by)

    assert caught.value.error_code == "MAKER_REVIEWER_CONFLICT"
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT uploaded_by FROM tax_master_version WHERE id = :version_id"),
            {"version_id": imported.version_ids[0]},
        ).scalar_one()
    assert stored == normalized


@pytest.mark.parametrize("identity", ["maker\u200b@example.com", "maker\n@example.com"])
def test_identity_control_and_format_characters_are_rejected(
    service_resources: tuple[TaxMasterService, Engine],
    identity: str,
) -> None:
    service, _ = service_resources

    with pytest.raises(InvalidImportOptionsError) as caught:
        service.import_xlsx(
            filename="identity-control.xlsx",
            payload=b"not-read",
            uploaded_by=identity,
        )

    assert caught.value.issues[0].error_code == "INVALID_IMPORT_OPTIONS"


def test_reviewer_control_character_is_rejected_before_approval(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-REVIEWER-CONTROL-{uuid4().hex}"
    _seed_company(engine, company_code, "Reviewer Control Company")
    imported = service.import_xlsx(
        filename="reviewer-control.xlsx",
        payload=_xlsx([_row(company_code, "Reviewer Control Company")]),
        uploaded_by="maker@example.com",
    )

    with pytest.raises(InvalidImportOptionsError):
        service.approve(
            imported.version_ids[0],
            reviewed_by="reviewer\u200b@example.com",
        )

    with engine.connect() as connection:
        status_value = connection.execute(
            text("SELECT status FROM tax_master_version WHERE id = :version_id"),
            {"version_id": imported.version_ids[0]},
        ).scalar_one()
    assert status_value == "DRAFT"


def test_filename_nfc_and_trim_drive_audit_and_idempotency(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-FILENAME-{uuid4().hex}"
    _seed_company(engine, company_code, "Filename Company")
    payload = _xlsx([_row(company_code, "Filename Company")])

    first = service.import_xlsx(
        filename="  cafe\u0301.xlsx  ",
        payload=payload,
        uploaded_by="maker@example.com",
    )
    replay = service.import_xlsx(
        filename="café.xlsx",
        payload=payload,
        uploaded_by="maker@example.com",
    )

    assert replay.replayed is True
    assert replay.batch_id == first.batch_id
    assert first.source_filename == "café.xlsx"
    with engine.connect() as connection:
        payload_ref = connection.execute(
            text("SELECT payload_ref FROM ingest_batch WHERE id = :batch_id"),
            {"batch_id": first.batch_id},
        ).scalar_one()
    assert payload_ref == "café.xlsx"


@pytest.mark.parametrize("filename", ["bad\x00.xlsx", "bad\u200b.xlsx"])
def test_filename_control_and_format_characters_are_rejected(
    service_resources: tuple[TaxMasterService, Engine],
    filename: str,
) -> None:
    service, _ = service_resources

    with pytest.raises(InvalidImportOptionsError) as caught:
        service.import_xlsx(
            filename=filename,
            payload=b"not-read",
            uploaded_by="maker@example.com",
        )

    assert caught.value.issues[0].field == "filename"


def test_invalid_xlsx_is_failed_audited_and_exact_replay_returns_original_issues(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    payload = b"not-an-xlsx-workbook"

    with pytest.raises(MasterDataValidationError) as first:
        service.import_xlsx(
            filename="broken.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
            currency="CNY",
            amount_scale=2,
        )
    with pytest.raises(MasterDataValidationError) as replay:
        service.import_xlsx(
            filename="broken.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
            currency="CNY",
            amount_scale=2,
        )

    assert first.value.batch_id is not None
    assert replay.value.batch_id == first.value.batch_id
    assert replay.value.issues == first.value.issues
    assert first.value.issues[0].error_code == "INVALID_XLSX"
    with engine.connect() as connection:
        batches = connection.execute(
            text(
                """
                SELECT id, status, payload_ref, checksum, currency, amount_scale,
                       record_count, accepted_count, rejected_count, control_total,
                       extraction_time, period, source_primary_key_definition
                FROM ingest_batch
                WHERE id = :batch_id
                """
            ),
            {"batch_id": first.value.batch_id},
        ).mappings().all()
        errors = connection.execute(
            text(
                """
                SELECT row_number, error_code, details
                FROM ingest_error
                WHERE batch_id = :batch_id
                ORDER BY row_number, id
                """
            ),
            {"batch_id": first.value.batch_id},
        ).mappings().all()

    assert len(batches) == 1
    batch = batches[0]
    assert batch["status"] == "FAILED"
    assert batch["payload_ref"] == "broken.xlsx"
    assert batch["checksum"] == sha256(payload).hexdigest()
    assert batch["currency"] == "CNY"
    assert batch["amount_scale"] == 2
    assert (batch["record_count"], batch["accepted_count"], batch["rejected_count"]) == (
        0,
        0,
        0,
    )
    assert batch["control_total"] == Decimal("0E-12")
    assert batch["extraction_time"].tzinfo is not None
    assert batch["period"] == batch["extraction_time"].date()
    assert batch["source_primary_key_definition"]["uploaded_by"] == "maker@example.com"
    assert batch["source_primary_key_definition"]["source_filename"] == "broken.xlsx"
    assert errors[0]["error_code"] == "INVALID_XLSX"


def test_parse_rejection_audits_real_control_total_when_every_loss_is_readable(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-PARSE-{uuid4().hex}"
    _seed_company(engine, company_code, "Parse Company")
    payload = _xlsx(
        [
            _row(
                company_code,
                "Parse Company",
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 9, 30),
                loss="100.25",
            ),
            _row(
                company_code,
                "Parse Company",
                valid_from=date(2026, 7, 1),
                loss="200.50",
            ),
        ]
    )

    with pytest.raises(MasterDataValidationError) as caught:
        service.import_xlsx(
            filename="overlap.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
            currency="CNY",
            amount_scale=2,
        )

    assert caught.value.issues[0].error_code == "OVERLAPPING_EFFECTIVE_PERIOD"
    assert caught.value.batch_id is not None
    with engine.connect() as connection:
        batch = connection.execute(
            text(
                """
                SELECT status, record_count, accepted_count, rejected_count, control_total
                FROM ingest_batch
                WHERE id = :batch_id
                """
            ),
            {"batch_id": caught.value.batch_id},
        ).mappings().one()
    assert batch["status"] == "FAILED"
    assert (batch["record_count"], batch["accepted_count"], batch["rejected_count"]) == (
        2,
        0,
        2,
    )
    assert batch["control_total"] == Decimal("300.750000000000")


def test_row_validation_error_is_failed_audited_with_explicit_zero_control_total(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-ROW-{uuid4().hex}"
    _seed_company(engine, company_code, "Row Error Company")

    with pytest.raises(MasterDataValidationError) as caught:
        service.import_xlsx(
            filename="row-error.xlsx",
            payload=_xlsx(
                [_row(company_code, "Row Error Company", loss="not-a-decimal")]
            ),
            uploaded_by="maker@example.com",
            currency="CNY",
            amount_scale=2,
        )

    assert caught.value.batch_id is not None
    assert caught.value.issues[0].error_code == "INVALID_LOSS_CARRYFORWARD"
    with engine.connect() as connection:
        audited = connection.execute(
            text(
                """
                SELECT batch.status, batch.record_count, batch.rejected_count,
                       batch.control_total, error.error_code
                FROM ingest_batch AS batch
                JOIN ingest_error AS error ON error.batch_id = batch.id
                WHERE batch.id = :batch_id
                """
            ),
            {"batch_id": caught.value.batch_id},
        ).mappings().one()
    assert audited["status"] == "FAILED"
    assert (audited["record_count"], audited["rejected_count"]) == (1, 1)
    assert audited["control_total"] == Decimal("0E-12")
    assert audited["error_code"] == "INVALID_LOSS_CARRYFORWARD"


def test_failed_replay_preserves_canonical_order_for_multiple_row_issues(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-MULTI-{uuid4().hex}"
    _seed_company(engine, company_code, "Multiple Error Company")
    payload = _xlsx(
        [
            _row(
                company_code,
                "Multiple Error Company",
                valid_to=date(2025, 12, 31),
                tax_rate="101%",
                burden="-1%",
            )
        ]
    )

    def rejected() -> MasterDataValidationError:
        with pytest.raises(MasterDataValidationError) as caught:
            service.import_xlsx(
                filename="multiple-errors.xlsx",
                payload=payload,
                uploaded_by="maker@example.com",
            )
        return caught.value

    first = rejected()
    replay = rejected()

    assert [(issue.field, issue.error_code) for issue in first.issues] == [
        ("tax_rate", "INVALID_RATE"),
        ("three_year_average_tax_burden", "INVALID_RATE"),
        ("valid_to", "INVALID_EFFECTIVE_PERIOD"),
    ]
    assert replay.batch_id == first.batch_id
    assert replay.issues == first.issues
    with engine.connect() as connection:
        batch_count = connection.execute(
            text(
                """
                SELECT count(*) FROM ingest_batch
                WHERE source = 'TAX_MASTER_XLSX'
                  AND payload_ref = 'multiple-errors.xlsx'
                  AND checksum = :checksum
                """
            ),
            {"checksum": sha256(payload).hexdigest()},
        ).scalar_one()
    assert batch_count == 1


def test_import_rejects_unknown_inactive_and_name_mismatch_as_one_atomic_file(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    prefix = uuid4().hex
    inactive = f"TM-I-{prefix}"
    mismatch = f"TM-M-{prefix}"
    unknown = f"TM-U-{prefix}"
    _seed_company(engine, inactive, "Inactive", lifecycle="INACTIVE")
    _seed_company(engine, mismatch, "Controlled Name")
    payload = _xlsx(
        [
            _row(inactive, "Inactive"),
            _row(mismatch, "Wrong Name"),
            _row(unknown, "Unknown"),
        ]
    )

    with pytest.raises(MasterDataValidationError) as caught:
        service.import_xlsx(
            filename="invalid.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
            currency="CNY",
            amount_scale=2,
        )

    assert [(issue.row_number, issue.error_code) for issue in caught.value.issues] == [
        (2, "INACTIVE_COMPANY"),
        (3, "COMPANY_NAME_MISMATCH"),
        (4, "UNKNOWN_COMPANY"),
    ]
    assert caught.value.batch_id is not None
    with engine.connect() as connection:
        version_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM tax_master_version AS version
                JOIN company ON company.id = version.company_id
                WHERE company.company_code IN (:inactive, :mismatch)
                """
            ),
            {"inactive": inactive, "mismatch": mismatch},
        ).scalar_one()
        failed_batch = connection.execute(
            text(
                """
                SELECT status, record_count, accepted_count, rejected_count, control_total
                FROM ingest_batch
                WHERE id = :batch_id
                """
            ),
            {"batch_id": caught.value.batch_id},
        ).mappings().one()
    assert version_count == 0
    assert failed_batch["status"] == "FAILED"
    assert (
        failed_batch["record_count"],
        failed_batch["accepted_count"],
        failed_batch["rejected_count"],
    ) == (3, 0, 3)
    assert failed_batch["control_total"] == Decimal("300.000000000000")


def test_approval_enforces_maker_reviewer_separation_and_lookup_is_point_in_time(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-A-{uuid4().hex}"
    _seed_company(engine, company_code, "Approval Company")
    imported = service.import_xlsx(
        filename="approval.xlsx",
        payload=_xlsx([_row(company_code, "Approval Company", burden="11%")]),
        uploaded_by="maker@example.com",
        currency="CNY",
        amount_scale=2,
    )

    with pytest.raises(MasterDataConflictError) as same_person:
        service.approve(imported.version_ids[0], reviewed_by="  maker@example.com  ")
    published = service.approve(imported.version_ids[0], reviewed_by="reviewer@example.com")
    with pytest.raises(MasterDataConflictError) as published_reapproval:
        service.approve(imported.version_ids[0], reviewed_by="second-reviewer@example.com")
    resolved = service.lookup(company_code, effective_on=date(2026, 6, 30))

    assert same_person.value.error_code == "MAKER_REVIEWER_CONFLICT"
    assert published_reapproval.value.error_code == "TAX_MASTER_STATE_CONFLICT"
    assert published.status == "PUBLISHED"
    assert published.approved_by == "reviewer@example.com"
    assert published.published_at is not None
    assert published.published_at.tzinfo is not None
    assert resolved.id == published.id
    assert str(resolved.three_year_average_tax_burden) == "0.110000000000"
    with pytest.raises(MasterDataNotFoundError) as missing:
        service.lookup(company_code, effective_on=date(2025, 12, 31))
    assert missing.value.error_code == "TAX_MASTER_NOT_FOUND"


def test_approval_rechecks_current_company_name_under_the_company_lock(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-NAME-{uuid4().hex}"
    _seed_company(engine, company_code, "Imported Company Name")
    imported = service.import_xlsx(
        filename="company-name.xlsx",
        payload=_xlsx([_row(company_code, "Imported Company Name")]),
        uploaded_by="maker@example.com",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE company
                SET company_name = 'Current Controlled Name', master_data_updated_at = now()
                WHERE company_code = :company_code
                """
            ),
            {"company_code": company_code},
        )

    with pytest.raises(MasterDataConflictError) as caught:
        service.approve(imported.version_ids[0], reviewed_by="reviewer@example.com")

    assert caught.value.error_code == "COMPANY_NAME_MISMATCH"
    with engine.connect() as connection:
        status_value = connection.execute(
            text("SELECT status FROM tax_master_version WHERE id = :version_id"),
            {"version_id": imported.version_ids[0]},
        ).scalar_one()
    assert status_value == "DRAFT"


def test_approval_flushes_and_refreshes_before_commit_with_no_post_commit_sql(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-COMMIT-ORDER-{uuid4().hex}"
    _seed_company(engine, company_code, "Commit Order Company")
    imported = service.import_xlsx(
        filename="commit-order.xlsx",
        payload=_xlsx([_row(company_code, "Commit Order Company")]),
        uploaded_by="maker@example.com",
    )
    events: list[str] = []

    class RecordingSession(Session):
        def flush(self, objects=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append("flush")
            super().flush(objects)

        def refresh(self, instance, attribute_names=None, with_for_update=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append("refresh")
            super().refresh(instance, attribute_names, with_for_update)

    factory = sessionmaker(
        bind=engine,
        class_=RecordingSession,
        autoflush=False,
        expire_on_commit=False,
    )

    class RecordingUow(UnitOfWork):
        def commit(self) -> None:
            events.append("commit")
            super().commit()

    approved = TaxMasterService(partial(RecordingUow, factory)).approve(
        imported.version_ids[0],
        reviewed_by="reviewer@example.com",
    )

    assert approved.status == "PUBLISHED"
    assert events[-3:] == ["flush", "refresh", "commit"]
    assert events.count("commit") == 1


def test_refresh_failure_rolls_back_before_approval_commit(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-REFRESH-FAIL-{uuid4().hex}"
    _seed_company(engine, company_code, "Refresh Failure Company")
    imported = service.import_xlsx(
        filename="refresh-failure.xlsx",
        payload=_xlsx([_row(company_code, "Refresh Failure Company")]),
        uploaded_by="maker@example.com",
    )
    events: list[str] = []

    class FailingRefreshSession(Session):
        def flush(self, objects=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append("flush")
            super().flush(objects)

        def refresh(self, instance, attribute_names=None, with_for_update=None) -> None:  # type: ignore[no-untyped-def,override]
            events.append("refresh")
            if isinstance(instance, TaxMasterVersion):
                raise RuntimeError("injected refresh failure")
            super().refresh(instance, attribute_names, with_for_update)

    factory = sessionmaker(
        bind=engine,
        class_=FailingRefreshSession,
        autoflush=False,
        expire_on_commit=False,
    )

    class RecordingUow(UnitOfWork):
        def commit(self) -> None:
            events.append("commit")
            super().commit()

    with pytest.raises(RuntimeError, match="injected refresh failure"):
        TaxMasterService(partial(RecordingUow, factory)).approve(
            imported.version_ids[0],
            reviewed_by="reviewer@example.com",
        )

    assert "refresh" in events
    assert "commit" not in events
    with engine.connect() as connection:
        persisted_status = connection.execute(
            text("SELECT status FROM tax_master_version WHERE id = :version_id"),
            {"version_id": imported.version_ids[0]},
        ).scalar_one()
    assert persisted_status == "DRAFT"


def test_overlapping_published_versions_are_rejected(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-O-{uuid4().hex}"
    _seed_company(engine, company_code, "Overlap Company")
    first = service.import_xlsx(
        filename="first.xlsx",
        payload=_xlsx(
            [
                _row(
                    company_code,
                    "Overlap Company",
                    valid_from=date(2026, 1, 1),
                    valid_to=date(2026, 9, 30),
                )
            ]
        ),
        uploaded_by="maker-one@example.com",
    )
    second = service.import_xlsx(
        filename="second.xlsx",
        payload=_xlsx(
            [_row(company_code, "Overlap Company", valid_from=date(2026, 7, 1), loss="101.00")]
        ),
        uploaded_by="maker-two@example.com",
    )

    service.approve(first.version_ids[0], reviewed_by="reviewer@example.com")
    with pytest.raises(MasterDataConflictError) as caught:
        service.approve(second.version_ids[0], reviewed_by="other-reviewer@example.com")

    assert caught.value.error_code == "PUBLISHED_PERIOD_OVERLAP"


def test_concurrent_overlapping_approvals_cannot_both_publish(
    service_resources: tuple[TaxMasterService, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine = service_resources
    company_code = f"TM-C-{uuid4().hex}"
    _seed_company(engine, company_code, "Concurrent Company")
    first = service.import_xlsx(
        filename="concurrent-one.xlsx",
        payload=_xlsx([_row(company_code, "Concurrent Company", loss="201.00")]),
        uploaded_by="maker-one@example.com",
    )
    second = service.import_xlsx(
        filename="concurrent-two.xlsx",
        payload=_xlsx([_row(company_code, "Concurrent Company", loss="202.00")]),
        uploaded_by="maker-two@example.com",
    )
    candidates_locked = Event()
    candidate_barrier = Barrier(2, action=candidates_locked.set)
    original_get = MasterRepository.get_tax_master

    def get_with_barrier(
        repository: MasterRepository,
        version_id,
        *,
        for_update: bool = False,
    ):
        version = original_get(repository, version_id, for_update=for_update)
        if for_update:
            candidate_barrier.wait(timeout=5)
        return version

    monkeypatch.setattr(MasterRepository, "get_tax_master", get_with_barrier)

    def approve(version_id: object, reviewer: str) -> str:
        try:
            service.approve(version_id, reviewed_by=reviewer)  # type: ignore[arg-type]
            return "PUBLISHED"
        except MasterDataConflictError as error:
            return error.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(approve, first.version_ids[0], "reviewer-one@example.com"),
            executor.submit(approve, second.version_ids[0], "reviewer-two@example.com"),
        ]
        outcomes = sorted(future.result() for future in futures)

    assert candidates_locked.is_set()
    assert outcomes == ["PUBLISHED", "PUBLISHED_PERIOD_OVERLAP"]
    with engine.connect() as connection:
        statuses = connection.execute(
            text(
                """
                SELECT status, count(*) FROM tax_master_version
                WHERE id IN (:first_id, :second_id)
                GROUP BY status ORDER BY status
                """
            ),
            {"first_id": first.version_ids[0], "second_id": second.version_ids[0]},
        ).all()
    assert statuses == [("DRAFT", 1), ("PUBLISHED", 1)]


def test_xlsx_parse_holds_no_database_connection(
    service_resources: tuple[TaxMasterService, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine = service_resources
    company_code = f"TM-NODB-{uuid4().hex}"
    _seed_company(engine, company_code, "No DB Parse Company")
    payload = _xlsx([_row(company_code, "No DB Parse Company")])
    entered = Event()
    release = Event()
    real_adapter = master_data_module.TaxMasterXlsxAdapter

    class BlockingAdapter:
        def __init__(self, source: bytes, *, amount_scale: int, limits: object) -> None:
            self._delegate = real_adapter(
                source,
                amount_scale=amount_scale,
                limits=limits,
            )

        def parse(self):
            entered.set()
            assert release.wait(timeout=5)
            return self._delegate.parse()

    monkeypatch.setattr(master_data_module, "TaxMasterXlsxAdapter", BlockingAdapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.import_xlsx,
            filename="no-db-parse.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
        )
        assert entered.wait(timeout=3)
        try:
            assert isinstance(engine.pool, QueuePool)
            assert engine.pool.checkedout() == 0
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            release.set()
        assert future.result(timeout=5).replayed is False


def test_concurrent_identical_imports_parse_together_then_persist_one_batch(
    service_resources: tuple[TaxMasterService, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine = service_resources
    company_code = f"TM-IMPORT-RACE-{uuid4().hex}"
    _seed_company(engine, company_code, "Import Race Company")
    payload = _xlsx([_row(company_code, "Import Race Company")])
    parse_barrier = Barrier(2)
    real_adapter = master_data_module.TaxMasterXlsxAdapter

    class BarrierAdapter:
        def __init__(self, source: bytes, *, amount_scale: int, limits: object) -> None:
            self._delegate = real_adapter(
                source,
                amount_scale=amount_scale,
                limits=limits,
            )

        def parse(self):
            parse_barrier.wait(timeout=3)
            return self._delegate.parse()

    monkeypatch.setattr(master_data_module, "TaxMasterXlsxAdapter", BarrierAdapter)

    def run_import():
        return service.import_xlsx(
            filename="same-concurrent.xlsx",
            payload=payload,
            uploaded_by="maker@example.com",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_import) for _ in range(2)]
        results = [future.result(timeout=8) for future in futures]

    assert len({result.batch_id for result in results}) == 1
    assert sorted(result.replayed for result in results) == [False, True]
    with engine.connect() as connection:
        batch_count = connection.execute(
            text(
                """
                SELECT count(*) FROM ingest_batch
                WHERE source = 'TAX_MASTER_XLSX'
                  AND payload_ref = 'same-concurrent.xlsx'
                  AND checksum = :checksum
                """
            ),
            {"checksum": sha256(payload).hexdigest()},
        ).scalar_one()
    assert batch_count == 1


def test_lookup_reports_conflict_if_persisted_data_has_multiple_matches(
    service_resources: tuple[TaxMasterService, Engine],
) -> None:
    service, engine = service_resources
    company_code = f"TM-DQ-{uuid4().hex}"
    _seed_company(engine, company_code, "Data Quality Company")
    first = service.import_xlsx(
        filename="dq-one.xlsx",
        payload=_xlsx([_row(company_code, "Data Quality Company", loss="301.00")]),
        uploaded_by="maker-one@example.com",
    )
    second = service.import_xlsx(
        filename="dq-two.xlsx",
        payload=_xlsx([_row(company_code, "Data Quality Company", loss="302.00")]),
        uploaded_by="maker-two@example.com",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE tax_master_version
                SET status = 'PUBLISHED', published_at = now(), approved_by = 'forced-test'
                WHERE id IN (:first_id, :second_id)
                """
            ),
            {"first_id": first.version_ids[0], "second_id": second.version_ids[0]},
        )

    with pytest.raises(MasterDataConflictError) as caught:
        service.lookup(company_code, effective_on=date(2026, 3, 31))

    assert caught.value.error_code == "MULTIPLE_PUBLISHED_TAX_MASTERS"

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from dataclasses import replace
from functools import partial
from io import StringIO
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tax_risk.adapters.ingest.hesi_business_entertainment_csv import (
    HesiBusinessEntertainmentCsvAdapter,
)
from tax_risk.adapters.ingest.oa_business_entertainment_csv import (
    OaBusinessEntertainmentCsvAdapter,
)
from tax_risk.adapters.ingest.oa_material_requisition_csv import (
    OaMaterialRequisitionCsvAdapter,
)
from tax_risk.adapters.ingest.oa_self_procurement_csv import (
    OaSelfProcurementCsvAdapter,
)
from tax_risk.adapters.ingest.sap_business_entertainment_csv import (
    SapBusinessEntertainmentCsvAdapter,
)
from tax_risk.application.ingest import (
    BatchMetadata,
    BatchStateConflictError,
    IngestService,
)
from tax_risk.persistence.ingest_models import IngestBatchStatus, IngestMode
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


def _csv(headers: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def _seed_company(engine: Engine, company_code: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:company_code, :company_name, 'ACTIVE')"
            ),
            {"company_code": company_code, "company_name": f"Company {company_code}"},
        )


def _service(isolated_database_url: str) -> tuple[IngestService, Engine]:
    engine, factory = create_session_factory(isolated_database_url)
    return IngestService(partial(UnitOfWork, factory)), engine


def _metadata(adapter: type, token: str) -> BatchMetadata:
    return BatchMetadata(
        source=adapter.DATASET_CODE.upper(),
        source_batch_key=token,
        dataset_code=adapter.DATASET_CODE,
        extraction_time=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
        period=date(2026, 3, 31),
        mode=IngestMode.FULL,
        schema_version=adapter.SCHEMA_VERSION,
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={"fields": list(adapter.PRIMARY_KEY_FIELDS)},
    )


def _source_cases(company_code: str) -> list[tuple[type, dict[str, object], Decimal]]:
    return [
        (
            SapBusinessEntertainmentCsvAdapter,
            {
                "company_code": company_code, "fiscal_year": 2026, "period": 3,
                "posting_date": "2026-03-18", "document_number": "510001",
                "line_item": "001", "current_account_code": "660201",
                "current_account_name": "业务招待费", "amount": "-120.50",
                "currency": "CNY", "summary": "冲销客户餐费", "assignment": "OA-1",
                "reference": "BX-1", "reversal_reference": "510000",
            },
            Decimal("-120.50"),
        ),
        (
            HesiBusinessEntertainmentCsvAdapter,
            {
                "company_code": company_code, "fiscal_year": 2026, "period": 3,
                "expense_claim_id": "BX-1", "line_id": "1", "expense_date": "2026-03-17",
                "amount": "120.50", "currency": "CNY", "summary": "客户餐费",
                "expense_reason": "客户来访", "recipient_category": "客户",
                "participant_count": 4, "related_oa_id": "OA-1",
                "sap_document_number": "510001", "sap_line_item": "001",
            },
            Decimal("120.50"),
        ),
        (
            OaBusinessEntertainmentCsvAdapter,
            {
                "company_code": company_code, "application_id": "OA-1", "line_id": "1",
                "application_date": "2026-03-10", "reason": "客户来访",
                "recipient_category": "客户", "participant_count": 4,
                "amount": "120.50", "currency": "CNY",
            },
            Decimal("120.50"),
        ),
        (
            OaSelfProcurementCsvAdapter,
            {
                "company_code": company_code, "application_id": "ZC-1", "line_id": "1",
                "purchase_date": "2026-03-11", "item_description": "伴手礼",
                "reason": "客户来访", "recipient_category": "客户", "amount": "88.00",
                "currency": "CNY", "parent_oa_id": "OA-1", "parent_hesi_id": "BX-1",
            },
            Decimal("88.00"),
        ),
        (
            OaMaterialRequisitionCsvAdapter,
            {
                "company_code": company_code, "requisition_id": "WL-1", "line_id": "1",
                "requisition_date": "2026-03-12", "material_description": "礼盒",
                "purpose": "客户来访", "recipient_category": "客户", "quantity": "2.000",
                "unit": "盒", "amount": "100.00", "currency": "CNY",
                "parent_oa_id": "OA-1", "parent_hesi_id": "BX-1",
            },
            Decimal("100.00"),
        ),
    ]


def test_all_five_sources_persist_batch_source_record_and_observation_lineage(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-SOURCE-{uuid4().hex}"
    _seed_company(engine, company_code)
    try:
        for adapter, row, expected_total in _source_cases(company_code):
            created = service.create_batch(_metadata(adapter, uuid4().hex))
            result = service.ingest_csv(
                created.batch.id,
                f"{adapter.DATASET_CODE}.csv",
                _csv(adapter.HEADER, [row]),
            )

            assert result.status == IngestBatchStatus.SUCCEEDED
            assert result.record_count == result.accepted_count == 1
            assert result.rejected_count == 0
            assert result.control_total == expected_total
            service.require_ready_business_entertainment_batch(result.id)

            with engine.connect() as connection:
                lineage = connection.execute(
                    text(
                        "SELECT source_record_id, ingest_batch_id "
                        "FROM sap_expense_voucher_observation WHERE ingest_batch_id = :batch_id "
                        "UNION ALL "
                        "SELECT source_record_id, ingest_batch_id "
                        "FROM business_entertainment_source_observation "
                        "WHERE ingest_batch_id = :batch_id"
                    ),
                    {"batch_id": result.id},
                ).mappings().one()
                source_batch_id = connection.execute(
                    text("SELECT batch_id FROM source_record WHERE id = :source_record_id"),
                    {"source_record_id": lineage["source_record_id"]},
                ).scalar_one()

            assert lineage["ingest_batch_id"] == result.id
            assert source_batch_id == result.id
    finally:
        engine.dispose()


def test_duplicate_source_key_creates_partial_batch_that_is_not_ready(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-PARTIAL-{uuid4().hex}"
    _seed_company(engine, company_code)
    adapter = OaBusinessEntertainmentCsvAdapter
    row = _source_cases(company_code)[2][1]
    try:
        created = service.create_batch(_metadata(adapter, uuid4().hex))
        result = service.ingest_csv(
            created.batch.id,
            "oa-business-entertainment.csv",
            _csv(adapter.HEADER, [row, dict(row)]),
        )

        assert result.status == IngestBatchStatus.PARTIAL
        assert (result.record_count, result.accepted_count, result.rejected_count) == (2, 1, 1)
        assert result.control_total == Decimal("120.50")
        assert result.errors[0].error_code == "DUPLICATE_SOURCE_RECORD_KEY"
        with pytest.raises(BatchStateConflictError, match="must be SUCCEEDED"):
            service.require_ready_business_entertainment_batch(result.id)
    finally:
        engine.dispose()


def test_material_requisition_without_amount_preserves_null_in_source_lineage(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-NO-AMOUNT-{uuid4().hex}"
    _seed_company(engine, company_code)
    adapter = OaMaterialRequisitionCsvAdapter
    row = dict(_source_cases(company_code)[4][1])
    row["amount"] = ""
    row["currency"] = ""
    try:
        created = service.create_batch(_metadata(adapter, uuid4().hex))
        result = service.ingest_csv(
            created.batch.id,
            "oa-material-requisition.csv",
            _csv(adapter.HEADER, [row]),
        )

        assert result.status == IngestBatchStatus.SUCCEEDED
        assert result.control_total == Decimal("0")
        with engine.connect() as connection:
            stored_amount = connection.execute(
                text("SELECT amount FROM source_record WHERE batch_id = :batch_id"),
                {"batch_id": result.id},
            ).scalar_one()
        assert stored_amount is None
    finally:
        engine.dispose()


def test_same_source_primary_key_can_be_reextracted_in_a_new_batch(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-REEXTRACT-{uuid4().hex}"
    _seed_company(engine, company_code)
    adapter = OaBusinessEntertainmentCsvAdapter
    row = _source_cases(company_code)[2][1]
    try:
        results = []
        for _ in range(2):
            created = service.create_batch(_metadata(adapter, uuid4().hex))
            results.append(
                service.ingest_csv(
                    created.batch.id,
                    "oa-business-entertainment.csv",
                    _csv(adapter.HEADER, [row]),
                )
            )

        assert [result.status for result in results] == [
            IngestBatchStatus.SUCCEEDED,
            IngestBatchStatus.SUCCEEDED,
        ]
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT count(*) FROM business_entertainment_source_observation "
                    "WHERE source_record_key = :source_record_key"
                ),
                {"source_record_key": f"{company_code}|OA-1|1"},
            ).scalar_one()
        assert count == 2
    finally:
        engine.dispose()


def test_source_observation_cannot_be_updated_or_deleted(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-IMMUTABLE-SOURCE-{uuid4().hex}"
    _seed_company(engine, company_code)
    adapter = SapBusinessEntertainmentCsvAdapter
    row = _source_cases(company_code)[0][1]
    try:
        created = service.create_batch(_metadata(adapter, uuid4().hex))
        result = service.ingest_csv(
            created.batch.id,
            "sap-business-entertainment.csv",
            _csv(adapter.HEADER, [row]),
        )

        with pytest.raises(DBAPIError, match="immutable_business_entertainment_observation"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE sap_expense_voucher_observation "
                        "SET summary = 'changed' WHERE ingest_batch_id = :batch_id"
                    ),
                    {"batch_id": result.id},
                )
        with pytest.raises(DBAPIError, match="immutable_business_entertainment_observation"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM sap_expense_voucher_observation "
                        "WHERE ingest_batch_id = :batch_id"
                    ),
                    {"batch_id": result.id},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "metadata_override",
    [
        {"schema_version": "wrong-schema-version"},
        {"source_primary_key_definition": {"fields": ["wrong_key"]}},
    ],
)
def test_business_source_batch_metadata_must_match_adapter_contract(
    isolated_database_url: str,
    metadata_override: dict[str, object],
) -> None:
    service, engine = _service(isolated_database_url)
    company_code = f"BE-CONTRACT-{uuid4().hex}"
    _seed_company(engine, company_code)
    adapter = OaBusinessEntertainmentCsvAdapter
    row = _source_cases(company_code)[2][1]
    try:
        metadata = replace(_metadata(adapter, uuid4().hex), **metadata_override)
        created = service.create_batch(metadata)

        with pytest.raises(BatchStateConflictError, match="adapter contract"):
            service.ingest_csv(
                created.batch.id,
                "oa-business-entertainment.csv",
                _csv(adapter.HEADER, [row]),
            )
    finally:
        engine.dispose()

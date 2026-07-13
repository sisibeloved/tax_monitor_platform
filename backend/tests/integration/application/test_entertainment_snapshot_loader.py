from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tax_risk.adapters.ingest.sap_business_entertainment_csv import (
    SapBusinessEntertainmentCsvAdapter,
)
from tax_risk.application.business_entertainment.source_loader import (
    EntertainmentSnapshotSourceLoader,
)
from tax_risk.application.ingest import IngestService
from tax_risk.application.snapshots import ExpectedSnapshotMember, SnapshotService
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tests.integration.application.test_business_entertainment_ingest import (
    _csv,
    _metadata,
)
from tests.integration.application.test_snapshot_publication import (
    PERIOD,
    _seed_quality_case,
    _validate,
)


def test_loader_rejects_unpublished_snapshot_set_without_evaluation_input(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    set_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO snapshot_set "
                    "(id, set_key, period, status, expected_member_count) "
                    "VALUES (:id, :key, :period, 'DRAFT', 100)"
                ),
                {"id": set_id, "key": f"draft-{uuid4().hex}", "period": date(2026, 6, 30)},
            )

        result = EntertainmentSnapshotSourceLoader(partial(UnitOfWork, factory)).load_sap_vouchers(
            snapshot_set_id=set_id,
            company_code="C001",
            period_end=date(2026, 6, 30),
        )

        assert result.records == ()
        assert [issue.error_code for issue in result.issues] == ["SNAPSHOT_SET_NOT_PUBLISHED"]
    finally:
        engine.dispose()


def test_loader_rejects_missing_set_instead_of_returning_safe_empty(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        result = EntertainmentSnapshotSourceLoader(partial(UnitOfWork, factory)).load_sap_vouchers(
            snapshot_set_id=uuid4(),
            company_code="C001",
            period_end=date(2026, 6, 30),
        )

        assert result.records == ()
        assert [issue.error_code for issue in result.issues] == ["SNAPSHOT_SET_NOT_FOUND"]
        assert datetime.now(timezone.utc).utcoffset() is not None
    finally:
        engine.dispose()


def test_publication_projects_ytd_sap_once_and_loader_never_reads_later_source(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    snapshot_service = SnapshotService(partial(UnitOfWork, factory))
    ingest_service = IngestService(partial(UnitOfWork, factory))
    loader = EntertainmentSnapshotSourceLoader(partial(UnitOfWork, factory))
    members: list[ExpectedSnapshotMember] = []
    first_company_code = ""
    try:
        for index in range(100):
            company_code, batch_id = _seed_quality_case(engine)
            validated = _validate(snapshot_service, company_code, batch_id)
            assert validated.snapshot is not None
            published_snapshot = snapshot_service.publish(validated.snapshot.id)
            members.append(
                ExpectedSnapshotMember(
                    company_id=published_snapshot.company_id,
                    snapshot_id=published_snapshot.id,
                )
            )
            if index == 0:
                first_company_code = company_code

        def ingest_sap(document_number: str) -> None:
            adapter = SapBusinessEntertainmentCsvAdapter
            created = ingest_service.create_batch(_metadata(adapter, uuid4().hex))
            row = {
                "company_code": first_company_code,
                "fiscal_year": 2026,
                "period": 3,
                "posting_date": "2026-03-18",
                "document_number": document_number,
                "line_item": "001",
                "current_account_code": "660201",
                "current_account_name": "业务招待费",
                "amount": "120.50",
                "currency": "CNY",
                "summary": "客户餐费",
                "assignment": "OA-1",
                "reference": "BX-1",
                "reversal_reference": "",
            }
            result = ingest_service.ingest_csv(
                created.batch.id,
                f"{document_number}.csv",
                _csv(adapter.HEADER, [row]),
            )
            assert result.control_total == Decimal("120.50")

        ingest_sap("510001")
        snapshot_set = snapshot_service.publish_set(
            set_key=f"be-loader-{uuid4().hex}",
            period=PERIOD,
            expected_members=members,
        )
        assert snapshot_set.published_at.utcoffset() is not None

        first = loader.load_sap_vouchers(
            snapshot_set_id=snapshot_set.id,
            company_code=first_company_code,
            period_end=PERIOD,
        )
        assert not first.issues
        assert [record.document_number for record in first.records] == ["510001"]
        assert first.records[0].snapshot_id == members[0].snapshot_id

        ingest_sap("510002")
        replayed = loader.load_sap_vouchers(
            snapshot_set_id=snapshot_set.id,
            company_code=first_company_code,
            period_end=PERIOD,
        )
        assert replayed == first

        projection_id = first.records[0].projection_id
        with pytest.raises(DBAPIError, match="immutable_business_entertainment_observation"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE sap_expense_voucher_snapshot_projection "
                        "SET company_code = 'CHANGED' WHERE id = :id"
                    ),
                    {"id": projection_id},
                )
        with pytest.raises(DBAPIError, match="immutable_snapshot: cannot attach"):
            with engine.begin() as connection:
                observation_id = connection.execute(
                    text(
                        "SELECT id FROM sap_expense_voucher_observation "
                        "WHERE document_number = '510002' AND company_code = :company_code"
                    ),
                    {"company_code": first_company_code},
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO sap_expense_voucher_snapshot_projection "
                        "(observation_id, snapshot_id, company_code, period) "
                        "VALUES (:observation_id, :snapshot_id, :company_code, :period)"
                    ),
                    {
                        "observation_id": observation_id,
                        "snapshot_id": members[0].snapshot_id,
                        "company_code": first_company_code,
                        "period": PERIOD,
                    },
                )
    finally:
        engine.dispose()

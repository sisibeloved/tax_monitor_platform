from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.master_repositories import MasterRepository
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


@pytest.fixture
def uow_resources(
    isolated_database_url: str,
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    database_engine, factory = create_session_factory(isolated_database_url)
    try:
        yield database_engine, factory
    finally:
        database_engine.dispose()


def _company_count(database_engine: Engine, company_code: str) -> int:
    with database_engine.connect() as verification_connection:
        return verification_connection.execute(
            text("SELECT count(*) FROM company WHERE company_code = :company_code"),
            {"company_code": company_code},
        ).scalar_one()


def _assert_pool_released(database_engine: Engine) -> None:
    assert isinstance(database_engine.pool, QueuePool)
    assert database_engine.pool.checkedout() == 0


def test_published_tax_master_prefers_latest_publication_over_version_text(
    connection: Connection,
) -> None:
    company_id = connection.execute(
        text(
            """
            INSERT INTO company (company_code, company_name)
            VALUES ('MASTER-ORDER', 'Master ordering company')
            RETURNING id
            """
        )
    ).scalar_one()
    batch_id = connection.execute(
        text(
            """
            INSERT INTO ingest_batch (
                source, source_batch_key, dataset_code, status, extraction_time, period,
                mode, schema_version, currency, amount_scale, record_count,
                accepted_count, rejected_count, control_total, checksum
            )
            VALUES (
                'TAX_MASTER', 'master-order', 'tax_master', 'SUCCEEDED', now(),
                DATE '2026-03-31', 'FULL', '1', 'CNY', 2, 2, 2, 0, 0,
                repeat('c', 64)
            )
            RETURNING id
            """
        )
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO tax_master_version (
                company_id, source_batch_id, valid_from, version, status, tax_rate,
                loss_carryforward, average_tax_burden_rate_3y, currency, amount_scale,
                data, published_at
            )
            VALUES
                (
                    :company_id, :batch_id, DATE '2026-01-01', 'v9', 'PUBLISHED',
                    0.25, 0, 0.10, 'CNY', 2, '{}'::jsonb,
                    TIMESTAMPTZ '2026-01-09 00:00:00+00'
                ),
                (
                    :company_id, :batch_id, DATE '2026-01-01', 'v10', 'PUBLISHED',
                    0.25, 0, 0.10, 'CNY', 2, '{}'::jsonb,
                    TIMESTAMPTZ '2026-01-10 00:00:00+00'
                )
            """
        ),
        {"company_id": company_id, "batch_id": batch_id},
    )

    with Session(bind=connection) as session:
        selected = MasterRepository(session).published_tax_master(
            company_id,
            date(2026, 3, 31),
        )

    assert selected is not None
    assert selected.version == "v10"


def test_unit_of_work_explicit_commit_persists_and_releases_connection(
    uow_resources: tuple[Engine, sessionmaker[Session]],
) -> None:
    database_engine, factory = uow_resources
    company_code = f"UOW-COMMIT-{uuid4().hex}"

    with UnitOfWork(factory) as unit_of_work:
        unit_of_work.ingest.add_company(
            Company(company_code=company_code, company_name="Committed company")
        )
        unit_of_work.commit()

    _assert_pool_released(database_engine)
    assert _company_count(database_engine, company_code) == 1
    _assert_pool_released(database_engine)


def test_unit_of_work_normal_exit_without_commit_rolls_back_and_releases_connection(
    uow_resources: tuple[Engine, sessionmaker[Session]],
) -> None:
    database_engine, factory = uow_resources
    company_code = f"UOW-NORMAL-ROLLBACK-{uuid4().hex}"

    with UnitOfWork(factory) as unit_of_work:
        unit_of_work.ingest.add_company(
            Company(company_code=company_code, company_name="Rolled back company")
        )
        unit_of_work.session.flush()

    _assert_pool_released(database_engine)
    assert _company_count(database_engine, company_code) == 0
    _assert_pool_released(database_engine)


def test_unit_of_work_exception_rolls_back_and_releases_connection(
    uow_resources: tuple[Engine, sessionmaker[Session]],
) -> None:
    database_engine, factory = uow_resources
    company_code = f"UOW-EXCEPTION-ROLLBACK-{uuid4().hex}"

    with pytest.raises(RuntimeError, match="force rollback"):
        with UnitOfWork(factory) as unit_of_work:
            unit_of_work.ingest.add_company(
                Company(company_code=company_code, company_name="Exception company")
            )
            unit_of_work.session.flush()
            raise RuntimeError("force rollback")

    _assert_pool_released(database_engine)
    assert _company_count(database_engine, company_code) == 0
    _assert_pool_released(database_engine)

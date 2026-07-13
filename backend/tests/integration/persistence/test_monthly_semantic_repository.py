from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.limited_scope import DuplicateScopeMetric
from tax_risk.domain.semantic.sap_voucher import AccountFamily
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


PERIOD_END = date(2026, 6, 30)


def test_scope_and_lines_read_only_the_exact_published_snapshot(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        snapshot_set_id, snapshot_id, company_code = _seed_monthly_set(engine)
        with UnitOfWork(factory) as uow:
            fact = uow.monthly_semantic.get_scope_fact(
                company_code,
                "2026-06",
                MonitorType.WELFARE,
                snapshot_set_id,
                snapshot_id,
            )
            lines = uow.monthly_semantic.load_snapshot_bound_sap_vouchers(
                snapshot_set_id=snapshot_set_id,
                account_family=AccountFamily.WELFARE,
                company_code=company_code,
                period_end=PERIOD_END,
            )

        assert fact.cumulative_expense == Decimal("140.01")
        assert fact.cumulative_base == Decimal("1000.00")
        assert fact.snapshot_id == snapshot_id
        assert [(line.document_number, line.amount) for line in lines] == [
            ("510001", Decimal("200.00")),
            ("510002", Decimal("-59.99")),
        ]
        assert all(line.snapshot_id == snapshot_id for line in lines)
        assert all(line.projection_id and line.source_record_id for line in lines)
    finally:
        engine.dispose()


def test_missing_metric_stays_missing_and_empty_sap_is_valid(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        snapshot_set_id, snapshot_id, company_code = _seed_monthly_set(
            engine,
            include_base=False,
            include_sap=False,
        )
        with UnitOfWork(factory) as uow:
            fact = uow.monthly_semantic.get_scope_fact(
                company_code,
                "2026-06",
                MonitorType.WELFARE,
                snapshot_set_id,
                snapshot_id,
            )
            lines = uow.monthly_semantic.load_snapshot_bound_sap_vouchers(
                snapshot_set_id=snapshot_set_id,
                account_family=AccountFamily.WELFARE,
                company_code=company_code,
                period_end=PERIOD_END,
            )

        assert fact.cumulative_expense == Decimal("140.01")
        assert fact.cumulative_base is None
        assert lines == []
    finally:
        engine.dispose()


def test_duplicate_scope_metric_is_rejected(isolated_database_url: str) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        snapshot_set_id, snapshot_id, company_code = _seed_monthly_set(
            engine,
            duplicate_expense=True,
        )
        with UnitOfWork(factory) as uow, pytest.raises(DuplicateScopeMetric):
            uow.monthly_semantic.get_scope_fact(
                company_code,
                "2026-06",
                MonitorType.WELFARE,
                snapshot_set_id,
                snapshot_id,
            )
    finally:
        engine.dispose()


def _seed_monthly_set(
    engine: Engine,
    *,
    include_base: bool = True,
    include_sap: bool = True,
    duplicate_expense: bool = False,
) -> tuple[UUID, UUID, str]:
    token = uuid4().hex[:12]
    company_code = f"P3-{token}-000"
    with engine.begin() as connection:
        master_batch_id = _batch(
            connection,
            token,
            "MASTER",
            "quarterly_metric",
            0,
            Decimal("0"),
        )
        expense_count = 2 if duplicate_expense else 1
        expense_batch_id = _batch(
            connection,
            token,
            "WELFARE",
            "WELFARE_YTD",
            expense_count,
            Decimal("141.01") if duplicate_expense else Decimal("140.01"),
        )
        base_batch_id = (
            _batch(
                connection,
                token,
                "SALARY",
                "SALARY_YTD",
                1,
                Decimal("1000.00"),
            )
            if include_base
            else None
        )
        sap_batch_id = (
            _batch(
                connection,
                token,
                "SAP-WELFARE",
                "SAP_WELFARE_DETAIL",
                2,
                Decimal("140.01"),
            )
            if include_sap
            else None
        )
        snapshot_set_id = connection.execute(
            text(
                "INSERT INTO snapshot_set (set_key, period, status, expected_member_count) "
                "VALUES (:key, :period, 'DRAFT', 100) RETURNING id"
            ),
            {"key": f"monthly-{token}", "period": PERIOD_END},
        ).scalar_one()
        first_snapshot_id: UUID | None = None
        first_company_id: UUID | None = None
        for index in range(100):
            code = f"P3-{token}-{index:03d}"
            company_id = connection.execute(
                text(
                    "INSERT INTO company (company_code, company_name) "
                    "VALUES (:code, :name) RETURNING id"
                ),
                {"code": code, "name": f"Company {index}"},
            ).scalar_one()
            master_id = connection.execute(
                text(
                    "INSERT INTO tax_master_version "
                    "(company_id, source_batch_id, valid_from, version, status, tax_rate, "
                    "loss_carryforward, average_tax_burden_rate_3y, currency, amount_scale, "
                    "data, published_at, approved_by, uploaded_by, source_row_number) "
                    "VALUES (:company_id, :batch_id, '2026-01-01', :version, 'PUBLISHED', "
                    "0.25, 0, 0.10, 'CNY', 2, '{}'::jsonb, now(), 'reviewer', 'maker', 2) "
                    "RETURNING id"
                ),
                {
                    "company_id": company_id,
                    "batch_id": master_batch_id,
                    "version": f"v-{index}",
                },
            ).scalar_one()
            snapshot_id = connection.execute(
                text(
                    "INSERT INTO accounting_snapshot "
                    "(company_id, tax_master_version_id, period, source_version_set_hash, "
                    "status, currency, amount_scale, record_count, control_total, checksum, "
                    "lineage) VALUES (:company_id, :master_id, :period, :hash, 'DRAFT', "
                    "'CNY', 2, 2, 1140.01, :checksum, '{}'::jsonb) RETURNING id"
                ),
                {
                    "company_id": company_id,
                    "master_id": master_id,
                    "period": PERIOD_END,
                    "hash": f"{index:064x}",
                    "checksum": f"{index + 1000:064x}",
                },
            ).scalar_one()
            if index == 0:
                first_snapshot_id = snapshot_id
                first_company_id = company_id
                _metric(
                    connection,
                    expense_batch_id,
                    company_id,
                    f"welfare-{token}-1",
                    "WELFARE_YTD",
                    Decimal("140.01"),
                )
                if duplicate_expense:
                    _metric(
                        connection,
                        expense_batch_id,
                        company_id,
                        f"welfare-{token}-2",
                        "WELFARE_YTD",
                        Decimal("1.00"),
                    )
                source_batches = [expense_batch_id]
                if base_batch_id is not None:
                    _metric(
                        connection,
                        base_batch_id,
                        company_id,
                        f"salary-{token}",
                        "SALARY_YTD",
                        Decimal("1000.00"),
                    )
                    source_batches.append(base_batch_id)
                if sap_batch_id is not None:
                    source_batches.append(sap_batch_id)
                    for document_number, amount, posting_date in (
                        ("510002", Decimal("-59.99"), "2026-06-20"),
                        ("510001", Decimal("200.00"), "2026-05-20"),
                    ):
                        _sap_line(
                            connection,
                            sap_batch_id,
                            company_id,
                            code,
                            snapshot_id,
                            document_number,
                            amount,
                            posting_date,
                        )
                for batch_id in source_batches:
                    connection.execute(
                        text(
                            "INSERT INTO snapshot_source "
                            "(snapshot_id, ingest_batch_id, source, source_version, "
                            "record_count, control_total, currency, amount_scale, lineage) "
                            "SELECT :snapshot_id, id, source, schema_version, record_count, "
                            "control_total, currency, amount_scale, '{}'::jsonb "
                            "FROM ingest_batch WHERE id = :batch_id"
                        ),
                        {"snapshot_id": snapshot_id, "batch_id": batch_id},
                    )
            connection.execute(
                text(
                    "UPDATE accounting_snapshot SET status = 'PUBLISHED', published_at = now() "
                    "WHERE id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )
            connection.execute(
                text(
                    "INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id) "
                    "VALUES (:set_id, :company_id, :snapshot_id)"
                ),
                {
                    "set_id": snapshot_set_id,
                    "company_id": company_id,
                    "snapshot_id": snapshot_id,
                },
            )
        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :set_id"),
            {"set_id": snapshot_set_id},
        )
    assert first_snapshot_id is not None and first_company_id is not None
    return snapshot_set_id, first_snapshot_id, company_code


def _batch(
    connection: Connection,
    token: str,
    source: str,
    dataset_code: str,
    count: int,
    total: Decimal,
) -> UUID:
    return connection.execute(
        text(
            "INSERT INTO ingest_batch "
            "(source, source_batch_key, dataset_code, status, extraction_time, period, mode, "
            "schema_version, currency, amount_scale, record_count, accepted_count, "
            "rejected_count, control_total, checksum) "
            "VALUES (:source, :key, :dataset, 'SUCCEEDED', now(), :period, 'FULL', 'v1', "
            "'CNY', 2, :count, :count, 0, :total, repeat('a', 64)) RETURNING id"
        ),
        {
            "source": source,
            "key": f"{source}-{token}",
            "dataset": dataset_code,
            "period": PERIOD_END,
            "count": count,
            "total": total,
        },
    ).scalar_one()


def _metric(
    connection: Connection,
    batch_id: UUID,
    company_id: UUID,
    source_key: str,
    metric_code: str,
    amount: Decimal,
) -> None:
    payload = json.dumps(
        {
            "company_code": "ignored-by-id-bound-query",
            "period": PERIOD_END.isoformat(),
            "currency": "CNY",
            "amount_scale": 2,
            "metric_code": metric_code,
            "amount": format(amount, "f"),
        },
        sort_keys=True,
    )
    connection.execute(
        text(
            "INSERT INTO source_record "
            "(batch_id, source_record_key, company_id, dataset_code, period, currency, "
            "amount_scale, amount, payload, lineage, extracted_at) "
            "VALUES (:batch_id, :key, :company_id, :dataset, :period, 'CNY', 2, :amount, "
            "CAST(:payload AS jsonb), '{}'::jsonb, now())"
        ),
        {
            "batch_id": batch_id,
            "key": source_key,
            "company_id": company_id,
            "dataset": metric_code,
            "period": PERIOD_END,
            "amount": amount,
            "payload": payload,
        },
    )


def _sap_line(
    connection: Connection,
    batch_id: UUID,
    company_id: UUID,
    company_code: str,
    snapshot_id: UUID,
    document_number: str,
    amount: Decimal,
    posting_date: str,
) -> None:
    source_record_id = connection.execute(
        text(
            "INSERT INTO source_record "
            "(batch_id, source_record_key, company_id, dataset_code, period, currency, "
            "amount_scale, amount, payload, lineage, extracted_at) "
            "VALUES (:batch_id, :key, :company_id, 'SAP_WELFARE_DETAIL', :period, 'CNY', 2, "
            ":amount, '{}'::jsonb, '{}'::jsonb, now()) RETURNING id"
        ),
        {
            "batch_id": batch_id,
            "key": f"{company_code}|2026|{document_number}|001",
            "company_id": company_id,
            "period": PERIOD_END,
            "amount": amount,
        },
    ).scalar_one()
    observation_id = connection.execute(
        text(
            "INSERT INTO sap_expense_voucher_observation "
            "(source_record_id, ingest_batch_id, source_record_key, company_code, fiscal_year, "
            "period, posting_date, document_number, line_item, current_account_code, "
            "current_account_name, amount, currency, summary, account_family) "
            "VALUES (:source_id, :batch_id, :key, :company_code, 2026, "
            "EXTRACT(MONTH FROM CAST(:posting_date AS date)), CAST(:posting_date AS date), "
            ":document_number, '001', '660205', '职工福利费', :amount, 'CNY', "
            "'客户商务宴请', 'WELFARE') RETURNING id"
        ),
        {
            "source_id": source_record_id,
            "batch_id": batch_id,
            "key": f"{company_code}|2026|{document_number}|001",
            "company_code": company_code,
            "posting_date": posting_date,
            "document_number": document_number,
            "amount": amount,
        },
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO sap_expense_voucher_snapshot_projection "
            "(observation_id, snapshot_id, company_code, period) "
            "VALUES (:observation_id, :snapshot_id, :company_code, :period)"
        ),
        {
            "observation_id": observation_id,
            "snapshot_id": snapshot_id,
            "company_code": company_code,
            "period": PERIOD_END,
        },
    )

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from tax_risk.domain.business_entertainment.evaluation import (
    SapLinkCoverageItem,
    SapLinkStatus,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


PERIOD_END = date(2026, 6, 30)


def _seed_coverage_graph(engine: Engine) -> tuple[str, UUID, UUID, UUID, UUID]:
    token = uuid4().hex
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:code, :name, 'ACTIVE') RETURNING id"
            ),
            {"code": f"COV-{token}", "name": f"Coverage {token}"},
        ).scalar_one()
        master_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'MASTER', :key, 'tax_master', 'SUCCEEDED', now(), :period,
                    'FULL', 'v1', 'CNY', 2, 0, 0, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"master-{token}", "period": PERIOD_END},
        ).scalar_one()
        master_id = connection.execute(
            text(
                """
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, source_file_name, source_checksum,
                    source_row_number, uploaded_by, data, published_at, approved_by
                ) VALUES (
                    :company_id, :batch_id, '2026-01-01', 'v1', 'PUBLISHED',
                    0.25, 0, 0.1, 'CNY', 2, 'master.xlsx', repeat('b', 64),
                    2, 'maker', '{}'::jsonb, now(), 'reviewer'
                ) RETURNING id
                """
            ),
            {"company_id": company_id, "batch_id": master_batch_id},
        ).scalar_one()
        snapshot_id = connection.execute(
            text(
                """
                INSERT INTO accounting_snapshot (
                    company_id, tax_master_version_id, period, source_version_set_hash,
                    status, currency, amount_scale, record_count, control_total,
                    checksum, lineage, published_at
                ) VALUES (
                    :company_id, :master_id, :period, repeat('c', 64), 'PUBLISHED',
                    'CNY', 2, 8, 0, repeat('d', 64), '{}'::jsonb, now()
                ) RETURNING id
                """
            ),
            {"company_id": company_id, "master_id": master_id, "period": PERIOD_END},
        ).scalar_one()
        sap_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'SAP', :key, 'sap_business_entertainment', 'SUCCEEDED', now(),
                    '2026-03-31', 'FULL', 'v1', 'CNY', 2, 2, 2, 0, 300,
                    repeat('e', 64)
                ) RETURNING id
                """
            ),
            {"key": f"sap-{token}"},
        ).scalar_one()
        hesi_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'HESI', :key, 'hesi_business_entertainment', 'SUCCEEDED', now(),
                    '2026-03-31', 'FULL', 'v1', 'CNY', 2, 1, 1, 0, 100,
                    repeat('f', 64)
                ) RETURNING id
                """
            ),
            {"key": f"hesi-{token}"},
        ).scalar_one()
        sap_source_ids: list[UUID] = []
        observation_ids: list[UUID] = []
        for index, amount in ((1, Decimal("100.00")), (2, Decimal("200.00"))):
            source_id = connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    ) VALUES (
                        :batch_id, :key, :company_id, 'sap_business_entertainment',
                        '2026-03-31', 'CNY', 2, :amount, '{}'::jsonb, '{}'::jsonb, now()
                    ) RETURNING id
                    """
                ),
                {
                    "batch_id": sap_batch_id,
                    "key": f"SAP-{index}",
                    "company_id": company_id,
                    "amount": amount,
                },
            ).scalar_one()
            sap_source_ids.append(source_id)
            observation_ids.append(
                connection.execute(
                    text(
                        """
                        INSERT INTO sap_expense_voucher_observation (
                            source_record_id, ingest_batch_id, source_record_key,
                            company_code, fiscal_year, period, posting_date,
                            document_number, line_item, current_account_code,
                            current_account_name, amount, currency, summary, account_family
                        ) VALUES (
                            :source_id, :batch_id, :key, :company_code, 2026, 3,
                            '2026-03-18', :document_number, '001', '660201',
                            '业务招待费', :amount, 'CNY', '客户餐费',
                            'BUSINESS_ENTERTAINMENT'
                        ) RETURNING id
                        """
                    ),
                    {
                        "source_id": source_id,
                        "batch_id": sap_batch_id,
                        "key": f"SAP-{index}",
                        "company_code": f"COV-{token}",
                        "document_number": f"51000{index}",
                        "amount": amount,
                    },
                ).scalar_one()
            )
        hesi_source_id = connection.execute(
            text(
                """
                INSERT INTO source_record (
                    batch_id, source_record_key, company_id, dataset_code, period,
                    currency, amount_scale, amount, payload, lineage, extracted_at
                ) VALUES (
                    :batch_id, 'HESI-1', :company_id, 'hesi_business_entertainment',
                    '2026-03-31', 'CNY', 2, 100, '{}'::jsonb, '{}'::jsonb, now()
                ) RETURNING id
                """
            ),
            {"batch_id": hesi_batch_id, "company_id": company_id},
        ).scalar_one()
        evidence_link_id = connection.execute(
            text(
                """
                INSERT INTO evidence_link (
                    company_code, source_record_id, target_record_id, relation_kind,
                    relation_quality, matched_field, snapshot_id
                ) VALUES (
                    :company_code, :source_id, :target_id, 'BUSINESS_TO_SAP',
                    'EXACT', 'reference', :snapshot_id
                ) RETURNING id
                """
            ),
            {
                "company_code": f"COV-{token}",
                "source_id": hesi_source_id,
                "target_id": sap_source_ids[0],
                "snapshot_id": snapshot_id,
            },
        ).scalar_one()
        for observation_id in observation_ids:
            connection.execute(
                text(
                    """
                    INSERT INTO sap_expense_voucher_snapshot_projection (
                        observation_id, snapshot_id, company_code, period
                    ) VALUES (:observation_id, :snapshot_id, :company_code, :period)
                    """
                ),
                {
                    "observation_id": observation_id,
                    "snapshot_id": snapshot_id,
                    "company_code": f"COV-{token}",
                    "period": PERIOD_END,
                },
            )
    return (
        f"COV-{token}",
        snapshot_id,
        observation_ids[0],
        observation_ids[1],
        evidence_link_id,
    )


def test_sap_coverage_persistence_is_complete_and_idempotent(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    (
        company_code,
        snapshot_id,
        linked_observation_id,
        unlinked_observation_id,
        evidence_link_id,
    ) = _seed_coverage_graph(engine)
    items = (
        SapLinkCoverageItem(
            company_code=company_code,
            period_end=PERIOD_END,
            sap_observation_id=linked_observation_id,
            document_number="510001",
            line_item="001",
            amount=Decimal("100.00"),
            currency="CNY",
            link_status=SapLinkStatus.LINKED,
            exact_evidence_link_id=evidence_link_id,
            evaluated_via_business_document=True,
            snapshot_id=snapshot_id,
        ),
        SapLinkCoverageItem(
            company_code=company_code,
            period_end=PERIOD_END,
            sap_observation_id=unlinked_observation_id,
            document_number="510002",
            line_item="001",
            amount=Decimal("200.00"),
            currency="CNY",
            link_status=SapLinkStatus.UNLINKED,
            exact_evidence_link_id=None,
            evaluated_via_business_document=False,
            snapshot_id=snapshot_id,
        ),
    )
    try:
        for _ in range(2):
            with UnitOfWork(factory) as uow:
                persisted = uow.business_entertainment_scope.persist_sap_link_coverages(items)
                uow.commit()
                assert len(persisted) == 2

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT document_number, line_item, amount, currency, link_status,
                           exact_evidence_link_id, evaluated_via_business_document
                    FROM sap_link_coverage
                    WHERE snapshot_id = :snapshot_id
                    ORDER BY document_number
                    """
                ),
                {"snapshot_id": snapshot_id},
            ).mappings().all()

        assert len(rows) == 2
        assert rows[0]["link_status"] == "LINKED"
        assert rows[0]["exact_evidence_link_id"] == evidence_link_id
        assert rows[0]["evaluated_via_business_document"] is True
        assert rows[1]["link_status"] == "UNLINKED"
        assert rows[1]["exact_evidence_link_id"] is None
        assert rows[1]["evaluated_via_business_document"] is False
        assert [(row["document_number"], row["line_item"], row["amount"]) for row in rows] == [
            ("510001", "001", Decimal("100.00")),
            ("510002", "001", Decimal("200.00")),
        ]
    finally:
        engine.dispose()

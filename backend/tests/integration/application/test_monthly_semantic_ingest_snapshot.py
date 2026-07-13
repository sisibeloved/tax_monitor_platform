from __future__ import annotations

from datetime import date, datetime, timezone
from functools import partial
from uuid import uuid4

from sqlalchemy import text

from tax_risk.adapters.ingest.csv_adapter import CSVAdapter
from tax_risk.adapters.ingest.sap_expense import (
    SapDonationCsvAdapter,
    SapWelfareCsvAdapter,
)
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SapExpenseVoucherRecord
from tax_risk.application.ingest import BatchMetadata, IngestService
from tax_risk.application.snapshots import MONTHLY_SEMANTIC_PROFILE, SnapshotService
from tax_risk.persistence.ingest_models import IngestMode
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


SAP_HEADER = (
    "company_code,fiscal_year,period,posting_date,document_number,line_item,"
    "current_account_code,current_account_name,amount,currency,summary,assignment,"
    "reference,reversal_reference\n"
)


def test_welfare_and_donation_adapters_reuse_the_shared_sap_contract() -> None:
    row = "1001,2026,6,2026-06-20,510001,001,660205,职工福利费,800.00,CNY,客户商务宴请,,,\n"

    welfare = tuple(SapWelfareCsvAdapter((SAP_HEADER + row).encode()).iter_rows())[0]
    donation = tuple(SapDonationCsvAdapter((SAP_HEADER + row).encode()).iter_rows())[0]

    assert isinstance(welfare.value, SapExpenseVoucherRecord)
    assert isinstance(donation.value, SapExpenseVoucherRecord)
    assert welfare.value.account_family is AccountFamily.WELFARE
    assert donation.value.account_family is AccountFamily.DONATION


def test_monthly_scope_metrics_use_the_existing_financial_contract() -> None:
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "welfare-1001,1001,2026,2026-06-30,CNY,2,WELFARE_YTD,140.01,"
        "2026-07-01T08:00:00Z\n"
    ).encode()

    adapted = tuple(CSVAdapter(payload, dataset_code="WELFARE_YTD").iter_rows())[0]

    assert adapted.error is None
    assert adapted.value is not None
    assert adapted.value.metric_code == "WELFARE_YTD"


def test_monthly_profile_ingests_all_sources_and_publishes_immutable_snapshot(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    ingest = IngestService(partial(UnitOfWork, factory))
    company_code = f"MONTHLY-{uuid4().hex[:12]}"
    period_end = date(2026, 6, 30)
    try:
        with engine.begin() as connection:
            company_id = connection.execute(
                text(
                    "INSERT INTO company (company_code, company_name) "
                    "VALUES (:code, 'Monthly Company') RETURNING id"
                ),
                {"code": company_code},
            ).scalar_one()

        batch_ids = []
        for metric_code, amount in (
            ("WELFARE_YTD", "140.01"),
            ("SALARY_YTD", "1000.00"),
            ("DONATION_YTD", "120.00"),
            ("PROFIT_YTD", "1000.00"),
        ):
            batch_ids.append(
                _ingest_metric(
                    ingest,
                    company_code=company_code,
                    period_end=period_end,
                    metric_code=metric_code,
                    amount=amount,
                )
            )
        for adapter, document_number in (
            (SapWelfareCsvAdapter, "510001"),
            (SapDonationCsvAdapter, "520001"),
        ):
            batch_ids.append(
                _ingest_sap(
                    ingest,
                    adapter=adapter,
                    company_code=company_code,
                    period_end=period_end,
                    document_number=document_number,
                )
            )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tax_master_version "
                    "(company_id, source_batch_id, valid_from, version, status, tax_rate, "
                    "loss_carryforward, average_tax_burden_rate_3y, currency, amount_scale, "
                    "data, published_at, approved_by, uploaded_by, source_row_number) "
                    "VALUES (:company_id, :batch_id, '2026-01-01', 'monthly-v1', "
                    "'PUBLISHED', 0.25, 0, 0.10, 'CNY', 2, '{}'::jsonb, now(), "
                    "'reviewer', 'maker', 2)"
                ),
                {"company_id": company_id, "batch_id": batch_ids[0]},
            )

        snapshots = SnapshotService(partial(UnitOfWork, factory))
        validated = snapshots.validate(
            company_code=company_code,
            period=period_end,
            source_batch_ids=batch_ids,
            profile=MONTHLY_SEMANTIC_PROFILE,
        )
        assert validated.valid, validated.issues
        assert validated.snapshot is not None
        published = snapshots.publish(validated.snapshot.id)

        assert published.status.value == "PUBLISHED"
        assert published.record_count == 4
        assert published.lineage["profile"] == MONTHLY_SEMANTIC_PROFILE
        assert len(published.lineage["sources"]) == 6
    finally:
        engine.dispose()


def _ingest_metric(
    ingest: IngestService,
    *,
    company_code: str,
    period_end: date,
    metric_code: str,
    amount: str,
):
    created = ingest.create_batch(
        BatchMetadata(
            source=metric_code,
            source_batch_key=uuid4().hex,
            dataset_code=metric_code,
            extraction_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
            period=period_end,
            mode=IngestMode.FULL,
            schema_version="monthly-metric-v1",
            currency="CNY",
            amount_scale=2,
            source_primary_key_definition={"fields": ["source_record_key"]},
        )
    )
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        f"{metric_code}-1,{company_code},2026,2026-06-30,CNY,2,{metric_code},"
        f"{amount},2026-07-01T00:00:00Z\n"
    ).encode()
    return ingest.ingest_csv(created.batch.id, f"{metric_code}.csv", payload).id


def _ingest_sap(
    ingest: IngestService,
    *,
    adapter: type[SapWelfareCsvAdapter] | type[SapDonationCsvAdapter],
    company_code: str,
    period_end: date,
    document_number: str,
):
    created = ingest.create_batch(
        BatchMetadata(
            source=adapter.DATASET_CODE,
            source_batch_key=uuid4().hex,
            dataset_code=adapter.DATASET_CODE,
            extraction_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
            period=period_end,
            mode=IngestMode.FULL,
            schema_version=adapter.SCHEMA_VERSION,
            currency="CNY",
            amount_scale=2,
            source_primary_key_definition={"fields": list(adapter.PRIMARY_KEY_FIELDS)},
        )
    )
    row = (
        f"{company_code},2026,6,2026-06-20,{document_number},001,660205,"
        "费用科目,10.00,CNY,测试摘要,,,\n"
    )
    result = ingest.ingest_csv(
        created.batch.id,
        f"{adapter.DATASET_CODE}.csv",
        (SAP_HEADER + row).encode(),
    )
    return result.id

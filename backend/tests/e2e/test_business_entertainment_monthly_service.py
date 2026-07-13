from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import partial
import json
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text

from tax_risk.adapters.model.fake_structured_client import FakeStructuredModelClient
from tax_risk.application.business_entertainment.production_pipeline import (
    DatabaseBusinessEntertainmentPipeline,
)
from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentMonthlyService,
    BusinessEntertainmentRunRequest,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


PERIOD_END = date(2037, 3, 31)


def _batch(
    connection: Connection,
    *,
    token: str,
    source: str,
    dataset_code: str,
    count: int,
    total: Decimal,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO ingest_batch (
                source, source_batch_key, dataset_code, status, extraction_time,
                period, mode, schema_version, currency, amount_scale, record_count,
                accepted_count, rejected_count, control_total, checksum
            ) VALUES (
                :source, :key, :dataset_code, 'SUCCEEDED', now(), :period,
                'FULL', 'v1', 'CNY', 2, :count, :count, 0, :total,
                repeat('a', 64)
            ) RETURNING id
            """
        ),
        {
            "source": source,
            "key": f"{source}-{token}",
            "dataset_code": dataset_code,
            "period": PERIOD_END,
            "count": count,
            "total": total,
        },
    ).scalar_one()


def _source_record(
    connection: Connection,
    *,
    batch_id: UUID,
    key: str,
    company_id: UUID | None,
    dataset_code: str,
    amount: Decimal,
    payload_sql: str = "'{}'::jsonb",
) -> UUID:
    return connection.execute(
        text(
            f"""
            INSERT INTO source_record (
                batch_id, source_record_key, company_id, dataset_code, period,
                currency, amount_scale, amount, payload, lineage, extracted_at
            ) VALUES (
                :batch_id, :key, :company_id, :dataset_code, :period,
                'CNY', 2, :amount, {payload_sql}, '{{}}'::jsonb, now()
            ) RETURNING id
            """
        ),
        {
            "batch_id": batch_id,
            "key": key,
            "company_id": company_id,
            "dataset_code": dataset_code,
            "period": PERIOD_END,
            "amount": amount,
        },
    ).scalar_one()


def _seed_pipeline(engine: Engine) -> tuple[str, UUID, UUID, str]:
    token = uuid4().hex[:10]
    company_code = f"PIPE-{token}"
    dictionary_version = f"accounts-{token}"
    versions = {
        "MODEL": f"model-{token}",
        "PROMPT": f"prompt-{token}",
        "CASE_LIBRARY": f"cases-{token}",
    }
    with engine.begin() as connection:
        master_batch_id = _batch(
            connection,
            token=token,
            source="MASTER",
            dataset_code="tax_master",
            count=100,
            total=Decimal("0"),
        )
        company_id = connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:code, :name, 'ACTIVE') RETURNING id"
            ),
            {"code": company_code, "name": f"Pipeline {token}"},
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
                    :company_id, :batch_id, '2037-01-01', 'v1', 'PUBLISHED',
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
                    company_id, tax_master_version_id, period,
                    source_version_set_hash, status, currency, amount_scale,
                    record_count, control_total, checksum, lineage, published_at
                ) VALUES (
                    :company_id, :master_id, :period, repeat('c', 64), 'DRAFT',
                    'CNY', 2, 5, 630, repeat('d', 64), '{}'::jsonb, NULL
                ) RETURNING id
                """
            ),
            {
                "company_id": company_id,
                "master_id": master_id,
                "period": PERIOD_END,
            },
        ).scalar_one()

        sap_batch_id = _batch(
            connection,
            token=token,
            source="SAP_BE",
            dataset_code="sap_business_entertainment",
            count=2,
            total=Decimal("390"),
        )
        hesi_batch_id = _batch(
            connection,
            token=token,
            source="HESI_BE",
            dataset_code="hesi_business_entertainment",
            count=1,
            total=Decimal("180"),
        )
        oa_batch_id = _batch(
            connection,
            token=token,
            source="OA_BE",
            dataset_code="oa_business_entertainment",
            count=2,
            total=Decimal("260"),
        )
        sap_source_ids: list[UUID] = []
        sap_observation_ids: list[UUID] = []
        for index, amount, summary in (
            (1, Decimal("180"), "内部会议餐"),
            (2, Decimal("210"), "无前置申请的客户餐费"),
        ):
            source_id = _source_record(
                connection,
                batch_id=sap_batch_id,
                key=f"SAP-{token}-{index}",
                company_id=company_id,
                dataset_code="sap_business_entertainment",
                amount=amount,
                payload_sql=f"jsonb_build_object('summary', '{summary}')",
            )
            sap_source_ids.append(source_id)
            sap_observation_ids.append(
                connection.execute(
                    text(
                        """
                        INSERT INTO sap_expense_voucher_observation (
                            source_record_id, ingest_batch_id, source_record_key,
                            company_code, fiscal_year, period, posting_date,
                            document_number, line_item, current_account_code,
                            current_account_name, amount, currency, summary,
                            account_family
                        ) VALUES (
                            :source_id, :batch_id, :key, :company_code, 2037, 3,
                            '2037-03-18', :document, '001', '660203',
                            '业务招待费', :amount, 'CNY', :summary,
                            'BUSINESS_ENTERTAINMENT'
                        ) RETURNING id
                        """
                    ),
                    {
                        "source_id": source_id,
                        "batch_id": sap_batch_id,
                        "key": f"SAP-{token}-{index}",
                        "company_code": company_code,
                        "document": f"51000{index}",
                        "amount": amount,
                        "summary": summary,
                    },
                ).scalar_one()
            )

        hesi_source_id = _source_record(
            connection,
            batch_id=hesi_batch_id,
            key=f"HESI-{token}",
            company_id=company_id,
            dataset_code="hesi_business_entertainment",
            amount=Decimal("180"),
            payload_sql=(
                "jsonb_build_object('summary', '内部会议餐', "
                "'expense_reason', '内部会议餐', "
                "'sap_document_number', '510001', 'sap_line_item', '001')"
            ),
        )
        connection.execute(
            text(
                """
                INSERT INTO business_entertainment_source_observation (
                    source_record_id, ingest_batch_id, dataset_code,
                    source_record_key, company_code, fiscal_year, period,
                    document_date, document_id, line_id, amount, currency
                ) VALUES (
                    :source_id, :batch_id, 'hesi_business_entertainment',
                    :key, :company_code, 2037, 3, '2037-03-15', 'H-1', '1',
                    180, 'CNY'
                )
                """
            ),
            {
                "source_id": hesi_source_id,
                "batch_id": hesi_batch_id,
                "key": f"HESI-{token}",
                "company_code": company_code,
            },
        )
        for index, amount, reason in (
            (1, Decimal("120"), "培训班用餐"),
            (2, Decimal("140"), "资料不足待补充"),
        ):
            source_id = _source_record(
                connection,
                batch_id=oa_batch_id,
                key=f"OA-{token}-{index}",
                company_id=company_id,
                dataset_code="oa_business_entertainment",
                amount=amount,
                payload_sql=f"jsonb_build_object('reason', '{reason}')",
            )
            connection.execute(
                text(
                    """
                    INSERT INTO business_entertainment_source_observation (
                        source_record_id, ingest_batch_id, dataset_code,
                        source_record_key, company_code, fiscal_year, period,
                        document_date, document_id, line_id, amount, currency
                    ) VALUES (
                        :source_id, :batch_id, 'oa_business_entertainment',
                        :key, :company_code, 2037, 3, '2037-03-16',
                        :document_id, '1', :amount, 'CNY'
                    )
                    """
                ),
                {
                    "source_id": source_id,
                    "batch_id": oa_batch_id,
                    "key": f"OA-{token}-{index}",
                    "company_code": company_code,
                    "document_id": f"OA-{index}",
                    "amount": amount,
                },
            )

        for batch_id, source, count, total in (
            (sap_batch_id, "SAP_BE", 2, Decimal("390")),
            (hesi_batch_id, "HESI_BE", 1, Decimal("180")),
            (oa_batch_id, "OA_BE", 2, Decimal("260")),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO snapshot_source (
                        snapshot_id, ingest_batch_id, source, source_version,
                        record_count, control_total, currency, amount_scale, lineage
                    ) VALUES (
                        :snapshot_id, :batch_id, :source, 'v1', :count, :total,
                        'CNY', 2, '{}'::jsonb
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "batch_id": batch_id,
                    "source": source,
                    "count": count,
                    "total": total,
                },
            )
        connection.execute(
            text(
                "UPDATE accounting_snapshot SET status = 'PUBLISHED', "
                "published_at = now() WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        for observation_id in sap_observation_ids:
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
                    "company_code": company_code,
                    "period": PERIOD_END,
                },
            )

        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (
                    set_key, period, status, expected_member_count
                ) VALUES (:key, :period, 'DRAFT', 100) RETURNING id
                """
            ),
            {"key": f"be-pipeline-{token}", "period": PERIOD_END},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (
                    snapshot_set_id, company_id, snapshot_id
                ) VALUES (:set_id, :company_id, :snapshot_id)
                """
            ),
            {
                "set_id": snapshot_set_id,
                "company_id": company_id,
                "snapshot_id": snapshot_id,
            },
        )
        connection.execute(
            text(
                """
                WITH filler_companies AS (
                    INSERT INTO company (company_code, company_name, lifecycle)
                    SELECT :prefix || lpad(series::text, 3, '0'),
                           'Pipeline filler ' || series::text, 'ACTIVE'
                    FROM generate_series(1, 99) AS series
                    RETURNING id
                ), filler_masters AS (
                    INSERT INTO tax_master_version (
                        company_id, source_batch_id, valid_from, version, status,
                        tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                        currency, amount_scale, source_file_name, source_checksum,
                        source_row_number, uploaded_by, data, published_at, approved_by
                    )
                    SELECT id, :batch_id, '2037-01-01', 'v1', 'PUBLISHED',
                           0.25, 0, 0.1, 'CNY', 2, 'master.xlsx', repeat('b', 64),
                           2, 'maker', '{}'::jsonb, now(), 'reviewer'
                    FROM filler_companies
                    RETURNING id, company_id
                ), filler_snapshots AS (
                    INSERT INTO accounting_snapshot (
                        company_id, tax_master_version_id, period,
                        source_version_set_hash, status, currency, amount_scale,
                        record_count, control_total, checksum, lineage, published_at
                    )
                    SELECT company_id, id, :period, repeat('c', 64), 'PUBLISHED',
                           'CNY', 2, 0, 0, repeat('d', 64), '{}'::jsonb, now()
                    FROM filler_masters
                    RETURNING id, company_id
                )
                INSERT INTO snapshot_set_member (
                    snapshot_set_id, company_id, snapshot_id
                )
                SELECT :set_id, company_id, id FROM filler_snapshots
                """
            ),
            {
                "prefix": f"PF-{token}-",
                "batch_id": master_batch_id,
                "period": PERIOD_END,
                "set_id": snapshot_set_id,
            },
        )
        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :set_id"),
            {"set_id": snapshot_set_id},
        )

        scope_batch_id = _batch(
            connection,
            token=token,
            source="BE_SCOPE",
            dataset_code="business_entertainment_company_scope",
            count=1,
            total=Decimal("0"),
        )
        scope_source_id = _source_record(
            connection,
            batch_id=scope_batch_id,
            key=company_code,
            company_id=company_id,
            dataset_code="business_entertainment_company_scope",
            amount=Decimal("0"),
        )
        scope_version_id = connection.execute(
            text(
                """
                INSERT INTO business_entertainment_scope_version (
                    batch_id, effective_from, effective_to, source_file_name,
                    file_checksum, uploader_id, reviewer_id, status, approved_at,
                    published_at, published_by
                ) VALUES (
                    :batch_id, '2037-01-01', '2037-12-31', 'scope.xlsx',
                    repeat('e', 64), 'maker', 'reviewer', 'PUBLISHED', now(),
                    now(), 'publisher'
                ) RETURNING id
                """
            ),
            {"batch_id": scope_batch_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO business_entertainment_scope_company (
                    version_id, company_id, source_record_id
                ) VALUES (:version_id, :company_id, :source_id)
                """
            ),
            {
                "version_id": scope_version_id,
                "company_id": company_id,
                "source_id": scope_source_id,
            },
        )

        dictionary_batch_id = _batch(
            connection,
            token=token,
            source="ACCOUNT_DICT",
            dataset_code="suggested_account_dictionary",
            count=2,
            total=Decimal("0"),
        )
        dictionary_id = connection.execute(
            text(
                """
                INSERT INTO suggested_account_dictionary_version (
                    batch_id, dictionary_version, effective_from, effective_to,
                    checksum, uploaded_by, reviewer_id, published_by, status,
                    approved_at, published_at
                ) VALUES (
                    :batch_id, :version, '2037-01-01', '2037-12-31',
                    repeat('f', 64), 'maker', NULL, NULL,
                    'DRAFT', NULL, NULL
                ) RETURNING id
                """
            ),
            {"batch_id": dictionary_batch_id, "version": dictionary_version},
        ).scalar_one()
        for account_id, labels in (
            (
                "MANUAL_REVIEW",
                ["CURRENT_ACCOUNT_REASONABLE", "INSUFFICIENT_EVIDENCE"],
            ),
            ("EMPLOYEE_EDUCATION", ["EMPLOYEE_EDUCATION"]),
        ):
            account_source_id = _source_record(
                connection,
                batch_id=dictionary_batch_id,
                key=f"{dictionary_version}|{account_id}",
                company_id=None,
                dataset_code="suggested_account_dictionary",
                amount=Decimal("0"),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO suggested_account_entry (
                        dictionary_version_id, source_record_id, account_id,
                        account_code, account_name, accounting_classification,
                        allowed_monitor_types, allowed_labels, status
                    ) VALUES (
                        :dictionary_id, :source_id, :account_id, :account_id,
                        :account_id, 'EXPENSE', '["BUSINESS_ENTERTAINMENT"]'::jsonb,
                        CAST(:labels AS jsonb), 'ACTIVE'
                    )
                    """
                ),
                {
                    "dictionary_id": dictionary_id,
                    "source_id": account_source_id,
                    "account_id": account_id,
                    "labels": json.dumps(labels),
                },
            )
        connection.execute(
            text(
                """
                UPDATE suggested_account_dictionary_version
                SET status = 'PUBLISHED', reviewer_id = 'reviewer',
                    published_by = 'publisher', approved_at = now(), published_at = now()
                WHERE id = :dictionary_id
                """
            ),
            {"dictionary_id": dictionary_id},
        )
        for artifact_type, version in versions.items():
            connection.execute(
                text(
                    """
                    INSERT INTO semantic_artifact_version (
                        artifact_type, version, checksum, storage_ref, deployment_id,
                        effective_from, effective_to, status, uploaded_by,
                        reviewer_id, published_by, approved_at, published_at
                    ) VALUES (
                        :artifact_type, :version, repeat('1', 64), :storage_ref,
                        :deployment_id, '2037-01-01', '2037-12-31', 'PUBLISHED',
                        'maker', 'reviewer', 'publisher', now(), now()
                    )
                    """
                ),
                {
                    "artifact_type": artifact_type,
                    "version": version,
                    "storage_ref": f"artifact://{version}",
                    "deployment_id": "be-model" if artifact_type == "MODEL" else None,
                },
            )
    return company_code, snapshot_set_id, snapshot_id, dictionary_version


def _responses() -> list[dict[str, object]]:
    return [
        {
            "semantic_label": "CURRENT_ACCOUNT_REASONABLE",
            "confidence_tier": "HIGH",
            "evidence_citations": [],
            "recommended_account_ids": ["MANUAL_REVIEW"],
            "rationale_summary": "现有证据显示当前科目可能合理。",
            "missing_evidence": [],
        },
        {
            "semantic_label": "EMPLOYEE_EDUCATION",
            "confidence_tier": "HIGH",
            "evidence_citations": [],
            "recommended_account_ids": ["EMPLOYEE_EDUCATION"],
            "rationale_summary": "现有证据显示该费用可能属于职工教育经费。",
            "missing_evidence": [],
        },
        {
            "semantic_label": "INSUFFICIENT_EVIDENCE",
            "confidence_tier": "LOW",
            "evidence_citations": [],
            "recommended_account_ids": ["MANUAL_REVIEW"],
            "rationale_summary": "现有证据不足，建议补充材料。",
            "missing_evidence": ["接待对象"],
        },
    ]


def test_database_pipeline_executes_and_replays_the_governed_monthly_flow(
    e2e_database_url: str | None,
) -> None:
    assert e2e_database_url is not None
    engine, factory = create_session_factory(e2e_database_url)
    try:
        company_code, snapshot_set_id, snapshot_id, dictionary_version = (
            _seed_pipeline(engine)
        )
        token = dictionary_version.removeprefix("accounts-")
        request = BusinessEntertainmentRunRequest(
            run_id=uuid4(),
            company_code=company_code,
            period_end=PERIOD_END,
            snapshot_set_id=snapshot_set_id,
            rule_version_id="business-entertainment-rule-v1",
            lexicon_version="v1",
            model_version_id=f"model-{token}",
            prompt_version_id=f"prompt-{token}",
            case_library_version_id=f"cases-{token}",
            account_dictionary_version_id=dictionary_version,
        )
        outcomes: list[dict[str, object]] = []
        for task_id in ("task-first", "task-replay"):
            pipeline = DatabaseBusinessEntertainmentPipeline(
                uow_factory=partial(UnitOfWork, factory),
                model_client=FakeStructuredModelClient(
                    _responses(),
                    environment="test",
                ),
            )
            outcomes.append(
                BusinessEntertainmentMonthlyService(pipeline).run_company(
                    request,
                    task_id=task_id,
                )
            )

        assert outcomes[0]["status"] == outcomes[1]["status"] == "SUCCEEDED"
        assert outcomes[0]["idempotency_key"] == outcomes[1]["idempotency_key"]
        assert outcomes[0]["source_record_count"] == 5
        assert outcomes[0]["sap_coverage_count"] == 2
        assert outcomes[0]["candidate_count"] == 3
        assert outcomes[0]["detection_count"] == 3
        assert outcomes[0]["evidence_task_count"] == 1
        assert outcomes[0]["risk_case_count"] == 1
        assert outcomes[0]["standalone_sap_count"] == 1

        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM evidence_link
                       WHERE snapshot_id = :snapshot_id) AS links,
                      (SELECT count(*) FROM business_entertainment_evaluation
                       WHERE snapshot_id = :snapshot_id) AS evaluations,
                      (SELECT count(*) FROM sap_link_coverage
                       WHERE snapshot_id = :snapshot_id) AS coverages,
                      (SELECT count(*) FROM semantic_detection_record
                       WHERE company_code = :company_code) AS detections,
                      (SELECT count(*) FROM semantic_evidence_task
                       WHERE company_code = :company_code) AS evidence_tasks,
                      (SELECT count(*) FROM risk_case AS r
                       JOIN company AS c ON c.id = r.company_id
                       WHERE c.company_code = :company_code
                         AND r.monitor_type = 'BUSINESS_ENTERTAINMENT') AS cases
                    """
                ),
                {"snapshot_id": snapshot_id, "company_code": company_code},
            ).mappings().one()
        assert counts == {
            "links": 1,
            "evaluations": 3,
            "coverages": 2,
            "detections": 3,
            "evidence_tasks": 1,
            "cases": 1,
        }
    finally:
        engine.dispose()

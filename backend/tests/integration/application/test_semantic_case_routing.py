from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
import pytest

from tax_risk.application.semantic.detection_router import (
    RoutingOutcome,
    SemanticCaseRouter,
)
from tax_risk.domain.semantic.contracts import SemanticDetection
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


def _seed_graph(engine: Engine) -> tuple[str, UUID, UUID, UUID, UUID, UUID]:
    token = uuid4().hex
    company_code = f"SEM-{token}"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:code, :name, 'ACTIVE') RETURNING id"
            ),
            {"code": company_code, "name": f"Semantic {token}"},
        ).scalar_one()
        master_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'MASTER', :key, 'tax_master', 'SUCCEEDED', now(), '2032-03-31',
                    'FULL', 'v1', 'CNY', 2, 0, 0, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"master-{token}"},
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
                    :company_id, :batch_id, '2032-01-01', 'v1', 'PUBLISHED',
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
                    :company_id, :master_id, '2032-03-31', repeat('c', 64), 'PUBLISHED',
                    'CNY', 2, 2, 0, repeat('d', 64), '{}'::jsonb, now()
                ) RETURNING id
                """
            ),
            {"company_id": company_id, "master_id": master_id},
        ).scalar_one()
        source_batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale, record_count,
                    accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'SEMANTIC_TEST', :key, 'hesi_business_entertainment', 'SUCCEEDED',
                    now(), '2032-03-31', 'FULL', 'v1', 'CNY', 2, 2, 2, 0, 200,
                    repeat('e', 64)
                ) RETURNING id
                """
            ),
            {"key": f"source-{token}"},
        ).scalar_one()
        business_source_id = connection.execute(
            text(
                """
                INSERT INTO source_record (
                    batch_id, source_record_key, company_id, dataset_code, period,
                    currency, amount_scale, amount, payload, lineage, extracted_at
                ) VALUES (
                    :batch_id, 'BUSINESS-1', :company_id, 'hesi_business_entertainment',
                    '2032-03-31', 'CNY', 2, 100, '{}'::jsonb, '{}'::jsonb, now()
                ) RETURNING id
                """
            ),
            {"batch_id": source_batch_id, "company_id": company_id},
        ).scalar_one()
        sap_source_id = connection.execute(
            text(
                """
                INSERT INTO source_record (
                    batch_id, source_record_key, company_id, dataset_code, period,
                    currency, amount_scale, amount, payload, lineage, extracted_at
                ) VALUES (
                    :batch_id, 'SAP-1', :company_id, 'sap_business_entertainment',
                    '2032-03-31', 'CNY', 2, 100, '{}'::jsonb, '{}'::jsonb, now()
                ) RETURNING id
                """
            ),
            {"batch_id": source_batch_id, "company_id": company_id},
        ).scalar_one()
        sap_observation_id = connection.execute(
            text(
                """
                INSERT INTO sap_expense_voucher_observation (
                    source_record_id, ingest_batch_id, source_record_key, company_code,
                    fiscal_year, period, posting_date, document_number, line_item,
                    current_account_code, current_account_name, amount, currency,
                    summary, account_family
                ) VALUES (
                    :source_id, :batch_id, 'SAP-1', :company_code, 2032, 3,
                    '2032-03-18', '510001', '001', '660203', '业务招待费', 100,
                    'CNY', '内部会议餐', 'BUSINESS_ENTERTAINMENT'
                ) RETURNING id
                """
            ),
            {
                "source_id": sap_source_id,
                "batch_id": source_batch_id,
                "company_code": company_code,
            },
        ).scalar_one()
        evidence_link_id = connection.execute(
            text(
                """
                INSERT INTO evidence_link (
                    company_code, source_record_id, target_record_id, relation_kind,
                    relation_quality, matched_field, snapshot_id
                ) VALUES (
                    :company_code, :business_source, :sap_source, 'BUSINESS_TO_SAP',
                    'EXACT', 'reference', :snapshot_id
                ) RETURNING id
                """
            ),
            {
                "company_code": company_code,
                "business_source": business_source_id,
                "sap_source": sap_source_id,
                "snapshot_id": snapshot_id,
            },
        ).scalar_one()
    return (
        company_code,
        company_id,
        snapshot_id,
        business_source_id,
        sap_observation_id,
        evidence_link_id,
    )


def _detection(
    *,
    company_code: str,
    snapshot_id: UUID,
    source_id: UUID,
    label: str,
    model_version: str,
    linked: bool = False,
    sap_observation_id: UUID | None = None,
    evidence_link_id: UUID | None = None,
) -> SemanticDetection:
    return SemanticDetection.model_validate(
        {
            "detection_key": f"{source_id}|{model_version}|{label}|{linked}",
            "candidate_key": f"candidate-{source_id}",
            "company_code": company_code,
            "fiscal_year": 2032,
            "period": 3,
            "source_mode": "SAP_LINKED" if linked else "BUSINESS_DOCUMENT_UNLINKED",
            "canonical_source_record_id": source_id,
            "sap_observation_id": sap_observation_id if linked else None,
            "sap_document_number": "510001" if linked else None,
            "sap_line_item": "001" if linked else None,
            "amount": Decimal("100"),
            "currency": "CNY",
            "snapshot_id": snapshot_id,
            "exact_evidence_link_id": evidence_link_id if linked else None,
            "versions": {
                "rule_version_id": "rule-v1",
                "model_version_id": model_version,
                "prompt_version_id": "prompt-v1",
                "case_library_version_id": "cases-v1",
                "account_dictionary_version": "accounts-v1",
            },
            "semantic_label": label,
            "confidence_tier": "HIGH",
            "evidence_refs": [],
            "recommended_account_ids": ["MANUAL_REVIEW"],
            "rationale_summary": "现有证据显示可能存在科目错入。",
            "missing_evidence": ["接待对象"] if label == "INSUFFICIENT_EVIDENCE" else [],
            "detected_at": datetime.now(timezone.utc),
        }
    )


def test_transactional_routing_selects_one_outcome_and_keeps_cases_idempotent(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, company_id, snapshot_id, source_id, sap_id, link_id = _seed_graph(engine)
        router = SemanticCaseRouter(partial(UnitOfWork, factory))
        suspicious = frozenset({"MEETING_EXPENSE", "EMPLOYEE_EDUCATION", "EMPLOYEE_WELFARE"})

        reasonable = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="CURRENT_ACCOUNT_REASONABLE",
                model_version="reasonable-v1",
            ),
            suspicious_labels=suspicious,
        )
        insufficient = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="INSUFFICIENT_EVIDENCE",
                model_version="insufficient-v1",
            ),
            suspicious_labels=suspicious,
        )
        unlinked_detection = _detection(
            company_code=company_code,
            snapshot_id=snapshot_id,
            source_id=source_id,
            label="MEETING_EXPENSE",
            model_version="risk-v1",
        )
        unlinked = router.route(unlinked_detection, suspicious_labels=suspicious)
        replay = router.route(unlinked_detection, suspicious_labels=suspicious)
        upgraded_model = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="MEETING_EXPENSE",
                model_version="risk-v2",
            ),
            suspicious_labels=suspicious,
        )
        linked = router.route(
            _detection(
                company_code=company_code,
                snapshot_id=snapshot_id,
                source_id=source_id,
                label="EMPLOYEE_EDUCATION",
                model_version="linked-v1",
                linked=True,
                sap_observation_id=sap_id,
                evidence_link_id=link_id,
            ),
            suspicious_labels=suspicious,
        )

        assert reasonable.outcome is RoutingOutcome.DETECTION_ONLY
        assert reasonable.evidence_task_id is None and reasonable.risk_case_id is None
        assert insufficient.outcome is RoutingOutcome.EVIDENCE_TASK
        assert insufficient.evidence_task_id is not None and insufficient.risk_case_id is None
        assert unlinked.outcome is RoutingOutcome.RISK_CASE
        assert unlinked.risk_case_id == replay.risk_case_id == upgraded_model.risk_case_id
        assert replay.detection_created is False and replay.case_created is False
        assert upgraded_model.detection_created is True and upgraded_model.case_created is False
        assert linked.risk_case_id is not None and linked.risk_case_id != unlinked.risk_case_id

        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM semantic_detection_record
                       WHERE company_code = :company_code
                         AND snapshot_id = :snapshot_id) AS detections,
                      (SELECT count(*) FROM semantic_evidence_task
                       WHERE company_code = :company_code) AS tasks,
                      (SELECT count(*) FROM risk_case
                       WHERE monitor_type = 'BUSINESS_ENTERTAINMENT'
                         AND company_id = :company_id) AS cases
                    """
                ),
                {
                    "company_code": company_code,
                    "company_id": company_id,
                    "snapshot_id": snapshot_id,
                },
            ).mappings().one()
            details = connection.execute(
                text(
                    """
                    SELECT d.source_mode, d.sap_link_status, d.sap_observation_id,
                           d.risk_amount_source, d.workflow_note
                    FROM business_entertainment_case_detail AS d
                    JOIN semantic_detection_record AS r
                      ON r.id = d.semantic_detection_id
                    WHERE r.company_code = :company_code
                      AND r.snapshot_id = :snapshot_id
                    ORDER BY d.source_mode
                    """
                ),
                {"company_code": company_code, "snapshot_id": snapshot_id},
            ).mappings().all()
        assert counts == {"detections": 5, "tasks": 1, "cases": 2}
        unlinked_detail = next(
            row for row in details if row["source_mode"] == "BUSINESS_DOCUMENT_UNLINKED"
        )
        assert unlinked_detail["sap_link_status"] == "PENDING_LOCATION"
        assert unlinked_detail["sap_observation_id"] is None
        assert unlinked_detail["risk_amount_source"] == "BUSINESS_DOCUMENT"
        assert unlinked_detail["workflow_note"] == "待定位SAP凭证"
    finally:
        engine.dispose()


def test_router_rejects_cross_company_evidence_references(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        company_code, _, snapshot_id, source_id, _, _ = _seed_graph(engine)
        with engine.begin() as connection:
            other_company_id = connection.execute(
                text(
                    "INSERT INTO company (company_code, company_name, lifecycle) "
                    "VALUES (:code, 'Other Company', 'ACTIVE') RETURNING id"
                ),
                {"code": f"OTHER-{uuid4().hex}"},
            ).scalar_one()
            batch_id = connection.execute(
                text("SELECT batch_id FROM source_record WHERE id = :source_id"),
                {"source_id": source_id},
            ).scalar_one()
            foreign_source_id = connection.execute(
                text(
                    """
                    INSERT INTO source_record (
                        batch_id, source_record_key, company_id, dataset_code, period,
                        currency, amount_scale, amount, payload, lineage, extracted_at
                    ) VALUES (
                        :batch_id, :key, :company_id, 'oa_business_entertainment',
                        '2032-03-31', 'CNY', 2, 10, '{}'::jsonb, '{}'::jsonb, now()
                    ) RETURNING id
                    """
                ),
                {
                    "batch_id": batch_id,
                    "key": f"FOREIGN-{uuid4().hex}",
                    "company_id": other_company_id,
                },
            ).scalar_one()
        payload = _detection(
            company_code=company_code,
            snapshot_id=snapshot_id,
            source_id=source_id,
            label="MEETING_EXPENSE",
            model_version="cross-company-v1",
        ).model_dump(mode="json")
        payload["evidence_refs"] = [
            {
                "evidence_id": "foreign-evidence",
                "field_name": "申请事由",
                "quoted_text": "内部会议餐",
                "source_record_id": str(foreign_source_id),
                "snapshot_id": str(snapshot_id),
            }
        ]
        detection = SemanticDetection.model_validate(payload)

        with pytest.raises(ValueError, match="evidence reference"):
            SemanticCaseRouter(partial(UnitOfWork, factory)).route(
                detection,
                suspicious_labels=frozenset({"MEETING_EXPENSE"}),
            )
    finally:
        engine.dispose()

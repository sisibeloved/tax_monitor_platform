from __future__ import annotations

from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy import text

from tax_risk.application.audit import AuditEventDraft, AuditService
from tax_risk.db import apply_principal_context
from tax_risk.persistence.export_models import ExportJob
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import AUDIT_ROLE, COMPANY_FINANCE_ROLE, GROUP_TAX_ROLE, Principal


def test_company_scoped_tables_have_forced_rls(engine) -> None:
    expected = {
        "accounting_snapshot",
        "audit_event",
        "business_entertainment_case_detail",
        "business_entertainment_evaluation",
        "business_entertainment_scope_company",
        "business_entertainment_source_observation",
        "company",
        "detection_record",
        "evidence_link",
        "export_job",
        "ingest_batch",
        "ingest_error",
        "monitoring_run_company",
        "risk_case",
        "review_action",
        "sap_expense_voucher_observation",
        "sap_expense_voucher_snapshot_projection",
        "sap_link_coverage",
        "semantic_model_call_audit",
        "source_record",
        "snapshot_set_member",
        "snapshot_source",
        "tax_master_version",
        "semantic_detection_record",
        "semantic_evidence_task",
    }
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
            ),
            {"tables": sorted(expected)},
        ).all()

    assert {name for name, enabled, forced in rows if enabled and forced} == expected


def test_application_role_is_non_privileged_and_company_rows_are_isolated(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    company_a = uuid4()
    company_b = uuid4()
    admin_engine, _ = create_session_factory(isolated_database_url)
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company (id, company_code, company_name, lifecycle)
                VALUES (:company_a, :code_a, '公司A', 'ACTIVE'),
                       (:company_b, :code_b, '公司B', 'ACTIVE')
                """
            ),
            {
                "company_a": company_a,
                "company_b": company_b,
                "code_a": f"RLS-A-{company_a.hex[:8]}",
                "code_b": f"RLS-B-{company_b.hex[:8]}",
            },
        )
    app_engine, factory = create_session_factory(rls_database_url)
    principal = _company_principal(company_a)
    try:
        with factory() as session:
            role = session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert role.rolsuper is False
            assert role.rolbypassrls is False
            apply_principal_context(session, principal)
            visible = set(session.scalars(text("SELECT id FROM company")))
            assert visible == {company_a}
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def test_json_company_scopes_require_every_company_to_be_authorized(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    company_a = uuid4()
    company_b = uuid4()
    single_job = uuid4()
    mixed_job = uuid4()
    admin_engine, _ = create_session_factory(isolated_database_url)
    with admin_engine.begin() as connection:
        for job_id, company_ids in (
            (single_job, f'["{company_a}"]'),
            (mixed_job, f'["{company_a}", "{company_b}"]'),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO export_job (
                        id, export_type, requester_subject, requester_roles,
                        company_ids, normalized_filters, filters_hash,
                        authorization_version, schema_version, status, expires_at
                    ) VALUES (
                        :id, 'BUSINESS_ENTERTAINMENT', 'company-exporter',
                        '["company-finance"]'::jsonb, CAST(:company_ids AS jsonb),
                        '{}'::jsonb, repeat('a', 64), repeat('b', 64),
                        'v1', 'QUEUED', now() + interval '1 hour'
                    )
                    """
                ),
                {"id": job_id, "company_ids": company_ids},
            )
    app_engine, factory = create_session_factory(rls_database_url)
    try:
        with factory() as session:
            apply_principal_context(session, _company_principal(company_a))
            visible = set(session.scalars(select(ExportJob.id)))
            assert visible == {single_job}
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def test_company_role_cannot_read_group_ingest_control_records(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    batch_id = uuid4()
    error_id = uuid4()
    admin_engine, _ = create_session_factory(isolated_database_url)
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    id, source, source_batch_key, dataset_code, status,
                    extraction_time, period, mode, schema_version, currency,
                    amount_scale, record_count, accepted_count, rejected_count,
                    control_total, checksum
                ) VALUES (
                    :batch_id, 'RLS_TEST', :source_batch_key, 'quarterly_metric',
                    'FAILED', now(), '2026-06-30', 'FULL', '1', 'CNY',
                    2, 1, 0, 1, 0, repeat('a', 64)
                )
                """
            ),
            {"batch_id": batch_id, "source_batch_key": f"rls-{batch_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO ingest_error (
                    id, batch_id, row_number, error_code, message, details, retryable
                ) VALUES (
                    :error_id, :batch_id, 1, 'INVALID_COMPANY',
                    'sensitive rejected-row metadata', '{}'::jsonb, false
                )
                """
            ),
            {"error_id": error_id, "batch_id": batch_id},
        )

    app_engine, factory = create_session_factory(rls_database_url)
    try:
        with factory() as session:
            apply_principal_context(session, _company_principal(uuid4()))
            assert (
                session.scalar(
                    text("SELECT count(*) FROM ingest_batch WHERE id = :id"), {"id": batch_id}
                )
                == 0
            )
            assert (
                session.scalar(
                    text("SELECT count(*) FROM ingest_error WHERE id = :id"), {"id": error_id}
                )
                == 0
            )
        with factory() as session:
            apply_principal_context(session, _group_principal())
            assert (
                session.scalar(
                    text("SELECT count(*) FROM ingest_batch WHERE id = :id"), {"id": batch_id}
                )
                == 1
            )
            assert (
                session.scalar(
                    text("SELECT count(*) FROM ingest_error WHERE id = :id"), {"id": error_id}
                )
                == 1
            )
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def test_company_role_cannot_read_other_company_review_actions(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    company_a = uuid4()
    company_b = uuid4()
    case_a = uuid4()
    case_b = uuid4()
    action_a = uuid4()
    action_b = uuid4()
    admin_engine, _ = create_session_factory(isolated_database_url)
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company (id, company_code, company_name, lifecycle)
                VALUES (:company_a, :code_a, '复核公司A', 'ACTIVE'),
                       (:company_b, :code_b, '复核公司B', 'ACTIVE')
                """
            ),
            {
                "company_a": company_a,
                "company_b": company_b,
                "code_a": f"REVIEW-A-{company_a.hex[:8]}",
                "code_b": f"REVIEW-B-{company_b.hex[:8]}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO risk_case (
                    id, fingerprint, company_id, monitor_type, status,
                    risk_amount, currency, amount_scale, risk_direction, priority
                ) VALUES
                    (:case_a, :fingerprint_a, :company_a, 'ACCRUAL_ACCURACY', 'NEW',
                     100, 'CNY', 2, 'UNDER_ACCRUAL', 3),
                    (:case_b, :fingerprint_b, :company_b, 'ACCRUAL_ACCURACY', 'NEW',
                     200, 'CNY', 2, 'UNDER_ACCRUAL', 3)
                """
            ),
            {
                "case_a": case_a,
                "case_b": case_b,
                "company_a": company_a,
                "company_b": company_b,
                "fingerprint_a": f"review-a-{case_a}",
                "fingerprint_b": f"review-b-{case_b}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO review_action (
                    id, risk_case_id, actor, actor_role, from_status,
                    action, to_status, reason
                ) VALUES
                    (:action_a, :case_a, 'reviewer-a', 'group-tax', 'NEW',
                     'REQUEST_EVIDENCE', 'EVIDENCE_REQUIRED', 'test-a'),
                    (:action_b, :case_b, 'reviewer-b', 'group-tax', 'NEW',
                     'REQUEST_EVIDENCE', 'EVIDENCE_REQUIRED', 'test-b')
                """
            ),
            {
                "action_a": action_a,
                "action_b": action_b,
                "case_a": case_a,
                "case_b": case_b,
            },
        )

    app_engine, factory = create_session_factory(rls_database_url)
    try:
        with factory() as session:
            apply_principal_context(session, _company_principal(company_a))
            visible = set(session.scalars(text("SELECT id FROM review_action")))
            assert visible == {action_a}
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def test_scoped_auditor_cannot_read_mixed_company_event(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    company_a = uuid4()
    company_b = uuid4()
    admin_engine, admin_factory = create_session_factory(isolated_database_url)
    admin_audit = AuditService(partial(UnitOfWork, admin_factory))
    group_principal = Principal(
        subject="group-auditor",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )
    single_event = admin_audit.append(
        AuditEventDraft(
            action="RISK_READ",
            entity_type="RISK_CASE",
            entity_id=uuid4(),
            principal=group_principal,
            company_ids=frozenset({company_a}),
            result="SUCCEEDED",
        )
    )
    admin_audit.append(
        AuditEventDraft(
            action="RISK_READ",
            entity_type="RISK_CASE",
            entity_id=uuid4(),
            principal=group_principal,
            company_ids=frozenset({company_a, company_b}),
            result="SUCCEEDED",
        )
    )
    app_engine, app_factory = create_session_factory(rls_database_url)
    audit_principal = Principal(
        subject="scoped-auditor",
        roles=frozenset({AUDIT_ROLE}),
        allowed_company_ids=frozenset({company_a}),
        organization_path="/audit/scoped",
    )
    try:
        total, events = AuditService(partial(UnitOfWork, app_factory)).search(
            audit_principal,
            page=1,
            page_size=20,
        )
        assert total == 1
        assert [event.id for event in events] == [single_event]
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def _company_principal(company_id: UUID) -> Principal:
    return Principal(
        subject="company-exporter",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/companies/scoped",
    )


def _group_principal() -> Principal:
    return Principal(
        subject="group-tax-reader",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )

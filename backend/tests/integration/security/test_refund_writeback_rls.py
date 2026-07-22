from __future__ import annotations

from datetime import date
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tax_risk.application.refund_writebacks import IncomeTaxRefundWritebackService
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.context import principal_context
from tax_risk.security.principal import Principal
from tax_risk.security.service_scope import (
    issue_service_scope_token,
    service_principal,
    verify_service_scope_token,
)
from tax_risk.workers.income_tax_refund_writebacks import (
    INCOME_TAX_REFUND_WRITEBACK_QUEUE,
)


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def write_status(self, company_code: str, desired_value: str) -> object:
        self.calls.append((company_code, desired_value))
        return object()


def test_non_bypassrls_refund_worker_can_only_read_and_write_its_signed_company(
    isolated_database_url: str,
    rls_database_url: str,
) -> None:
    admin_engine, _admin_factory = create_session_factory(isolated_database_url)
    seeded = _seed_refund_rows(admin_engine)
    app_engine, app_factory = create_session_factory(rls_database_url)
    company_a = seeded["A"]["company_id"]
    company_b = seeded["B"]["company_id"]
    principal = _signed_worker_principal(company_a)
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        partial(UnitOfWork, app_factory),
        sender,
        max_retries=3,
    )
    try:
        with principal_context(principal):
            own = service.deliver(
                seeded["A"]["writeback_id"],
                expected_company_id=company_a,
            )
            other = service.deliver(
                seeded["B"]["writeback_id"],
                expected_company_id=company_b,
            )

        assert own.status == "SUCCEEDED"
        assert other.status == "NOT_FOUND"
        assert sender.calls == [("REFUND-RLS-A", "已退税")]

        with principal_context(principal):
            with UnitOfWork(app_factory) as uow:
                role = uow.session.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                ).one()
                assert tuple(role) == (False, False)

                visible = {
                    table_name: set(
                        uow.session.execute(text(f"SELECT id FROM {table_name}")).scalars()
                    )
                    for table_name in (
                        "income_tax_refund_target",
                        "income_tax_refund_scan_result",
                        "income_tax_refund_writeback",
                        "sap_gl_line_observation",
                    )
                }
                assert visible == {
                    "income_tax_refund_target": {seeded["A"]["target_id"]},
                    "income_tax_refund_scan_result": {seeded["A"]["scan_id"]},
                    "income_tax_refund_writeback": {seeded["A"]["writeback_id"]},
                    "sap_gl_line_observation": {seeded["A"]["line_id"]},
                }

                own_updates = (
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_target SET source_version = 'rls-own' "
                        "WHERE id = :id",
                        seeded["A"]["target_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_scan_result "
                        "SET structured_output = structured_output || "
                        "'{\"rls_write\": true}'::jsonb WHERE id = :id",
                        seeded["A"]["scan_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_writeback "
                        "SET desired_value = '已退税-RLS' WHERE id = :id",
                        seeded["A"]["writeback_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE sap_gl_line_observation SET gl_account_name = 'Own RLS' "
                        "WHERE id = :id",
                        seeded["A"]["line_id"],
                    ),
                )
                cross_updates = (
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_target SET source_version = 'forbidden' "
                        "WHERE id = :id",
                        seeded["B"]["target_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_scan_result "
                        "SET structured_output = structured_output || "
                        "'{\"forbidden\": true}'::jsonb WHERE id = :id",
                        seeded["B"]["scan_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE income_tax_refund_writeback SET desired_value = 'forbidden' "
                        "WHERE id = :id",
                        seeded["B"]["writeback_id"],
                    ),
                    _update_count(
                        uow.session,
                        "UPDATE sap_gl_line_observation SET gl_account_name = 'forbidden' "
                        "WHERE id = :id",
                        seeded["B"]["line_id"],
                    ),
                )
                uow.commit()

        assert own_updates == (1, 1, 1, 1)
        assert cross_updates == (0, 0, 0, 0)

        with principal_context(principal):
            with pytest.raises(DBAPIError, match="row-level security"):
                with UnitOfWork(app_factory) as uow:
                    uow.session.execute(
                        text(
                            "INSERT INTO income_tax_refund_target ("
                            "id, company_id, refund_tax_year, source_record_key, "
                            "expected_amount, currency, amount_scale, source_version, "
                            "receipt_status) VALUES ("
                            ":id, :company_id, 2024, :source_key, 1.00, 'CNY', 2, "
                            "'cross-company', 'PENDING')"
                        ),
                        {
                            "id": uuid4(),
                            "company_id": company_b,
                            "source_key": f"cross-company-{uuid4()}",
                        },
                    )
                    uow.commit()
    finally:
        app_engine.dispose()
        admin_engine.dispose()


def _update_count(session: Session, statement: str, row_id: UUID) -> int:
    result = session.execute(text(statement), {"id": row_id})
    return cast(CursorResult[Any], result).rowcount


def _signed_worker_principal(company_id: UUID) -> Principal:
    secret = "refund-rls-integration-worker-scope"
    batch_id = str(uuid4())
    token = issue_service_scope_token(
        secret=secret,
        queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
        run_type="INCOME_TAX_REFUND_WRITEBACK",
        batch_id=batch_id,
        company_ids=frozenset({company_id}),
        period=date(2026, 3, 31),
    )
    scope = verify_service_scope_token(
        token,
        secret=secret,
        expected_queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
        expected_run_type="INCOME_TAX_REFUND_WRITEBACK",
        expected_batch_id=batch_id,
    )
    return service_principal(scope)


def _seed_refund_rows(engine: Engine) -> dict[str, dict[str, UUID]]:
    seeded: dict[str, dict[str, UUID]] = {}
    with engine.begin() as connection:
        for suffix in ("A", "B"):
            ids = {
                "company_id": uuid4(),
                "target_id": uuid4(),
                "line_id": uuid4(),
                "scan_id": uuid4(),
                "writeback_id": uuid4(),
            }
            seeded[suffix] = ids
            batch_key = f"refund-rls-{suffix.lower()}-{uuid4()}"
            connection.execute(
                text(
                    "INSERT INTO company (id, company_code, company_name, lifecycle) "
                    "VALUES (:id, :code, :name, 'ACTIVE')"
                ),
                {
                    "id": ids["company_id"],
                    "code": f"REFUND-RLS-{suffix}",
                    "name": f"Refund RLS {suffix}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO sap_refund_evidence_batch ("
                    "source_batch_key, fiscal_year, through_period, company_ids, "
                    "status, record_count, checksum) VALUES ("
                    ":batch_key, 2026, '2026-03-31', CAST(:company_ids AS jsonb), "
                    "'COMPLETE', 1, repeat(:checksum_character, 64))"
                ),
                {
                    "batch_key": batch_key,
                    "company_ids": f'["{ids["company_id"]}"]',
                    "checksum_character": suffix.lower(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO income_tax_refund_target ("
                    "id, company_id, refund_tax_year, source_record_key, expected_amount, "
                    "currency, amount_scale, source_version, receipt_status, received_at, "
                    "latest_scan_period) VALUES ("
                    ":id, :company_id, 2025, :source_key, 100.00, 'CNY', 2, 'rls-v1', "
                    "'RECEIVED', now(), '2026-03-31')"
                ),
                {
                    "id": ids["target_id"],
                    "company_id": ids["company_id"],
                    "source_key": f"target-{suffix}-{uuid4()}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO sap_gl_line_observation ("
                    "id, company_id, source_batch_key, client, ledger, fiscal_year, "
                    "fiscal_period, posting_date, document_number, line_item, "
                    "gl_account_code, gl_account_name, account_category, debit_credit, "
                    "amount, currency, amount_scale, is_reversed, source_hash) VALUES ("
                    ":id, :company_id, :batch_key, '100', '0L', 2026, 3, '2026-03-15', "
                    ":document_number, '001', '6801010000', :account_name, "
                    "'INCOME_TAX_EXPENSE', 'DEBIT', 100.00, 'CNY', 2, false, "
                    "repeat(:hash_character, 64))"
                ),
                {
                    "id": ids["line_id"],
                    "company_id": ids["company_id"],
                    "batch_key": batch_key,
                    "document_number": f"RLS-{suffix}-001",
                    "account_name": f"Income tax {suffix}",
                    "hash_character": suffix.lower(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO income_tax_refund_scan_result ("
                    "id, target_id, company_id, scan_period, receipt_status, account_status, "
                    "matched_line_id, expected_amount, matched_amount, gl_account_code, "
                    "gl_account_name, alert_code, structured_output) VALUES ("
                    ":id, :target_id, :company_id, '2026-03-31', 'NOT_RECEIVED', "
                    "'NOT_APPLICABLE', NULL, 100.00, NULL, NULL, NULL, NULL, "
                    "jsonb_build_object('completeness', true, "
                    "'source_batch_key', CAST(:batch_key AS text)))"
                ),
                {
                    "id": ids["scan_id"],
                    "target_id": ids["target_id"],
                    "company_id": ids["company_id"],
                    "batch_key": batch_key,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO income_tax_refund_writeback ("
                    "id, target_id, company_id, idempotency_key, desired_value, status, "
                    "attempt_count) VALUES ("
                    ":id, :target_id, :company_id, :idempotency_key, '已退税', "
                    "'PENDING', 0)"
                ),
                {
                    "id": ids["writeback_id"],
                    "target_id": ids["target_id"],
                    "company_id": ids["company_id"],
                    "idempotency_key": f"refund-rls:{ids['target_id']}",
                },
            )
    return seeded

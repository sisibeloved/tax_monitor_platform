from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def _configure_background_connection(
    connection: Connection,
    application_name: str,
) -> None:
    connection.execute(
        text("SELECT set_config('application_name', :application_name, true)"),
        {"application_name": application_name},
    )
    connection.execute(text("SELECT set_config('lock_timeout', '5s', true)"))


def _wait_for_database_lock(
    engine: Engine,
    application_name: str,
    future: Future[None],
) -> None:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        if future.done():
            future.result()

        with engine.connect() as monitoring_connection:
            wait_event_type = monitoring_connection.execute(
                text(
                    """
                    SELECT activity.wait_event_type
                    FROM pg_stat_activity AS activity
                    WHERE activity.datname = current_database()
                      AND activity.application_name = :application_name
                      AND EXISTS (
                          SELECT 1
                          FROM pg_locks AS lock
                          WHERE lock.pid = activity.pid
                            AND NOT lock.granted
                      )
                    """
                ),
                {"application_name": application_name},
            ).scalar_one_or_none()

        if wait_event_type == "Lock":
            return
        sleep(0.01)

    raise AssertionError(f"database session {application_name!r} did not wait on a lock")


def _insert_company(connection: Connection, code: str = "1001") -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO company (company_code, company_name)
            VALUES (:code, :name)
            RETURNING id
            """
        ),
        {"code": code, "name": f"Company {code}"},
    ).scalar_one()


def _insert_batch(
    connection: Connection,
    *,
    source: str = "SAP",
    source_batch_key: str = "sap-2026-q1-v1",
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO ingest_batch (
                source, source_batch_key, dataset_code, status, extraction_time, period,
                mode, schema_version, currency, amount_scale, record_count,
                accepted_count, rejected_count, control_total, checksum
            )
            VALUES (
                :source, :source_batch_key, 'quarterly_metric', 'SUCCEEDED', now(),
                DATE '2026-03-31', 'FULL', '1', 'CNY', 2, 1, 1, 0, 10.00,
                repeat('a', 64)
            )
            RETURNING id
            """
        ),
        {"source": source, "source_batch_key": source_batch_key},
    ).scalar_one()


def _insert_tax_master(
    connection: Connection,
    company_id: UUID,
    *,
    batch_id: UUID | None = None,
) -> UUID:
    source_batch_id = batch_id or _insert_batch(
        connection,
        source="TAX_MASTER",
        source_batch_key=f"master-{company_id}",
    )
    return connection.execute(
        text(
            """
            INSERT INTO tax_master_version (
                company_id, source_batch_id, valid_from, version, status, tax_rate,
                loss_carryforward, average_tax_burden_rate_3y, currency, amount_scale,
                data, published_at, approved_by, uploaded_by, source_row_number
            )
            VALUES (
                :company_id, :batch_id, DATE '2026-01-01', 'v1', 'PUBLISHED', 0.25,
                0, 0.10, 'CNY', 2, '{}'::jsonb, now(),
                'legacy-test-reviewer', 'legacy-test-maker', 2
            )
            RETURNING id
            """
        ),
        {"company_id": company_id, "batch_id": source_batch_id},
    ).scalar_one()


@pytest.mark.parametrize(
    ("status", "loss", "published_at", "approved_by"),
    [
        ("DRAFT", "-0.01", None, None),
        ("PUBLISHED", "0", "2026-01-01T00:00:00+00:00", None),
        ("DRAFT", "0", None, "premature-reviewer"),
    ],
)
def test_tax_master_rejects_negative_loss_and_inconsistent_approval_state(
    connection: Connection,
    status: str,
    loss: str,
    published_at: str | None,
    approved_by: str | None,
) -> None:
    company_id = _insert_company(connection, code=f"MASTER-GOV-{uuid4().hex}")
    batch_id = _insert_batch(
        connection,
        source="TAX_MASTER",
        source_batch_key=f"master-gov-{uuid4().hex}",
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, data, published_at, approved_by,
                    uploaded_by, source_row_number
                )
                VALUES (
                    :company_id, :batch_id, DATE '2026-01-01', :version, :status,
                    0.25, :loss, 0.10, 'CNY', 2, '{}'::jsonb,
                    CAST(:published_at AS timestamptz), :approved_by, 'maker', 2
                )
                """
            ),
            {
                "company_id": company_id,
                "batch_id": batch_id,
                "version": uuid4().hex,
                "status": status,
                "loss": loss,
                "published_at": published_at,
                "approved_by": approved_by,
            },
        )


def _insert_snapshot(
    connection: Connection,
    company_id: UUID,
    *,
    status: str = "DRAFT",
    version_hash: str = "a" * 64,
    tax_master_version_id: UUID | None = None,
) -> UUID:
    published_at = "now()" if status == "PUBLISHED" else "NULL"
    master_version_id = tax_master_version_id or _insert_tax_master(connection, company_id)
    return connection.execute(
        text(
            f"""
            INSERT INTO accounting_snapshot (
                company_id, tax_master_version_id, period, source_version_set_hash,
                status, currency, amount_scale, record_count, control_total, checksum,
                published_at
            )
            VALUES (
                :company_id, :tax_master_version_id, DATE '2026-03-31',
                :version_hash, :status,
                'CNY', 2, 1, 10.00, repeat('b', 64), {published_at}
            )
            RETURNING id
            """
        ),
        {
            "company_id": company_id,
            "tax_master_version_id": master_version_id,
            "version_hash": version_hash,
            "status": status,
        },
    ).scalar_one()


def _insert_snapshot_set(
    connection: Connection,
    *,
    set_key: str,
    expected_member_count: int = 100,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO snapshot_set (set_key, period, status, expected_member_count)
            VALUES (:set_key, DATE '2026-03-31', 'DRAFT', :expected_member_count)
            RETURNING id
            """
        ),
        {"set_key": set_key, "expected_member_count": expected_member_count},
    ).scalar_one()


def _insert_rule_version(connection: Connection, *, rule_code: str = "RULE-Q1") -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO rule_version (
                rule_code, version, status, effective_from, definition,
                change_reason, published_at
            )
            VALUES (
                :rule_code, 'v1', 'PUBLISHED', DATE '2026-01-01', '{}'::jsonb,
                'Initial test rule', now()
            )
            RETURNING id
            """
        ),
        {"rule_code": rule_code},
    ).scalar_one()


def _insert_monitoring_run(
    connection: Connection,
    snapshot_set_id: UUID,
    rule_version_id: UUID,
    *,
    run_key: str = "RUN-Q1",
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO monitoring_run (
                run_key, run_type, snapshot_set_id, rule_version_id, status,
                fiscal_year, quarter, requested_company_count
            )
            VALUES (
                :run_key, 'QUARTERLY', :snapshot_set_id, :rule_version_id, 'PENDING',
                2026, 1, 1
            )
            RETURNING id
            """
        ),
        {
            "run_key": run_key,
            "snapshot_set_id": snapshot_set_id,
            "rule_version_id": rule_version_id,
        },
    ).scalar_one()


def _insert_detection(
    connection: Connection,
    *,
    detection_key: str,
    run_id: UUID,
    company_id: UUID,
    snapshot_id: UUID,
    rule_version_id: UUID,
    tax_master_version_id: UUID,
    calculation_status: str = "CALCULATED",
    result_amount: object = "10.00",
    not_calculated_reason: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO detection_record (
                detection_key, run_id, company_id, snapshot_id, rule_version_id,
                tax_master_version_id, monitor_type, calculation_status, input_amount,
                result_amount, difference_amount, rate_value, currency, amount_scale,
                formula_substitution, lineage, structured_output, not_calculated_reason
            )
            VALUES (
                :detection_key, :run_id, :company_id, :snapshot_id, :rule_version_id,
                :tax_master_version_id, 'ACCRUAL_ACCURACY', :calculation_status, 10.00,
                :result_amount, 0, 0.10, 'CNY', 2,
                '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :not_calculated_reason
            )
            RETURNING id
            """
        ),
        {
            "detection_key": detection_key,
            "run_id": run_id,
            "company_id": company_id,
            "snapshot_id": snapshot_id,
            "rule_version_id": rule_version_id,
            "tax_master_version_id": tax_master_version_id,
            "calculation_status": calculation_status,
            "result_amount": result_amount,
            "not_calculated_reason": not_calculated_reason,
        },
    ).scalar_one()


def _populate_snapshot_set(
    connection: Connection,
    snapshot_set_id: UUID,
    *,
    member_count: int,
    company_code_prefix: str = "SET-",
) -> list[UUID]:
    if member_count == 0:
        return []

    master_batch_id = _insert_batch(
        connection,
        source="TAX_MASTER",
        source_batch_key=f"snapshot-set-master-{snapshot_set_id}",
    )
    member_ids: list[UUID] = []
    for index in range(member_count):
        company_id = _insert_company(
            connection,
            code=f"{company_code_prefix}{index:03d}",
        )
        tax_master_version_id = _insert_tax_master(
            connection,
            company_id,
            batch_id=master_batch_id,
        )
        snapshot_id = _insert_snapshot(
            connection,
            company_id,
            status="PUBLISHED",
            tax_master_version_id=tax_master_version_id,
        )
        member_ids.append(
            connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                    VALUES (:snapshot_set_id, :company_id, :snapshot_id)
                    RETURNING id
                    """
                ),
                {
                    "snapshot_set_id": snapshot_set_id,
                    "company_id": company_id,
                    "snapshot_id": snapshot_id,
                },
            ).scalar_one()
        )
    return member_ids


def _insert_source_in_new_transaction(
    engine: Engine,
    snapshot_id: UUID,
    batch_id: UUID,
    application_name: str,
) -> None:
    with engine.begin() as connection:
        _configure_background_connection(connection, application_name)
        connection.execute(
            text(
                """
                INSERT INTO snapshot_source (
                    snapshot_id, ingest_batch_id, source, source_version, record_count,
                    control_total, currency, amount_scale, lineage
                )
                VALUES (
                    :snapshot_id, :batch_id, 'SAP', 'late-v1', 1, 10.00,
                    'CNY', 2, '{}'::jsonb
                )
                """
            ),
            {"snapshot_id": snapshot_id, "batch_id": batch_id},
        )


def _insert_member_in_new_transaction(
    engine: Engine,
    snapshot_set_id: UUID,
    company_id: UUID,
    snapshot_id: UUID,
    application_name: str,
) -> None:
    with engine.begin() as connection:
        _configure_background_connection(connection, application_name)
        connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                VALUES (:snapshot_set_id, :company_id, :snapshot_id)
                """
            ),
            {
                "snapshot_set_id": snapshot_set_id,
                "company_id": company_id,
                "snapshot_id": snapshot_id,
            },
        )


def _publish_snapshot_in_new_transaction(
    engine: Engine,
    snapshot_id: UUID,
    application_name: str,
) -> None:
    with engine.begin() as connection:
        _configure_background_connection(connection, application_name)
        connection.execute(
            text(
                """
                UPDATE accounting_snapshot
                SET status = 'PUBLISHED', published_at = now()
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        )


def _publish_snapshot_set_in_new_transaction(
    engine: Engine,
    snapshot_set_id: UUID,
    application_name: str,
) -> None:
    with engine.begin() as connection:
        _configure_background_connection(connection, application_name)
        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :snapshot_set_id"),
            {"snapshot_set_id": snapshot_set_id},
        )


def _assert_integrity_error(
    connection: Connection, statement: str, parameters: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(text(statement), parameters)


def test_company_code_is_unique_and_inactive_lifecycle_requires_audit_time(
    connection: Connection,
) -> None:
    _insert_company(connection)

    _assert_integrity_error(
        connection,
        "INSERT INTO company (company_code, company_name) VALUES ('1001', 'Duplicate')",
        {},
    )


def test_inactive_company_requires_deactivated_at(connection: Connection) -> None:
    _assert_integrity_error(
        connection,
        """
        INSERT INTO company (company_code, company_name, lifecycle)
        VALUES ('1002', 'Inactive without audit time', 'INACTIVE')
        """,
        {},
    )


@pytest.mark.parametrize(
    ("first_source", "first_key", "second_source", "second_key"),
    [("SAP", "q1", "SAP", "q1")],
)
def test_ingest_source_and_source_batch_key_are_unique(
    connection: Connection,
    first_source: str,
    first_key: str,
    second_source: str,
    second_key: str,
) -> None:
    _insert_batch(connection, source=first_source, source_batch_key=first_key)

    with pytest.raises(IntegrityError):
        _insert_batch(connection, source=second_source, source_batch_key=second_key)


def test_source_record_key_is_unique_within_a_batch(connection: Connection) -> None:
    company_id = _insert_company(connection)
    batch_id = _insert_batch(connection)
    statement = text(
        """
        INSERT INTO source_record (
            batch_id, source_record_key, company_id, dataset_code, period,
            currency, amount_scale, amount, payload, lineage, extracted_at
        )
        VALUES (
            :batch_id, 'row-1', :company_id, 'quarterly_metric', DATE '2026-03-31',
            'CNY', 2, 10.00, '{}'::jsonb, '{}'::jsonb, now()
        )
        """
    )
    connection.execute(statement, {"batch_id": batch_id, "company_id": company_id})

    with pytest.raises(IntegrityError):
        connection.execute(statement, {"batch_id": batch_id, "company_id": company_id})


def test_source_record_rejects_an_unknown_company_foreign_key(connection: Connection) -> None:
    batch_id = _insert_batch(connection)

    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO source_record (
                    batch_id, source_record_key, company_id, dataset_code, period,
                    currency, amount_scale, amount, payload, lineage, extracted_at
                )
                VALUES (
                    :batch_id, 'missing-company', :company_id, 'quarterly_metric',
                    DATE '2026-03-31', 'CNY', 2, 10.00, '{}'::jsonb, '{}'::jsonb, now()
                )
                """
            ),
            {"batch_id": batch_id, "company_id": uuid4()},
        )


def test_tax_master_company_valid_from_and_version_are_unique(connection: Connection) -> None:
    company_id = _insert_company(connection)
    batch_id = _insert_batch(connection)
    statement = text(
        """
        INSERT INTO tax_master_version (
            company_id, source_batch_id, valid_from, version, status, tax_rate,
            loss_carryforward, average_tax_burden_rate_3y, currency, amount_scale,
            data, uploaded_by, source_row_number
        )
        VALUES (
            :company_id, :batch_id, DATE '2026-01-01', 'v1', 'DRAFT', 0.25,
            0, 0.10, 'CNY', 2, '{}'::jsonb, 'unique-test-maker', 2
        )
        """
    )
    parameters = {"company_id": company_id, "batch_id": batch_id}
    connection.execute(statement, parameters)

    with pytest.raises(IntegrityError):
        connection.execute(statement, parameters)


def test_accounting_snapshot_source_version_set_is_unique_per_company_and_period(
    connection: Connection,
) -> None:
    company_id = _insert_company(connection)
    _insert_snapshot(connection, company_id)

    with pytest.raises(IntegrityError):
        _insert_snapshot(connection, company_id)


def test_accounting_snapshot_rejects_tax_master_from_another_company(
    connection: Connection,
) -> None:
    snapshot_company_id = _insert_company(connection, code="SNAPSHOT-COMPANY")
    master_company_id = _insert_company(connection, code="MASTER-COMPANY")
    other_company_master_id = _insert_tax_master(connection, master_company_id)

    with pytest.raises(IntegrityError):
        _insert_snapshot(
            connection,
            snapshot_company_id,
            tax_master_version_id=other_company_master_id,
        )


@pytest.mark.parametrize("mismatch", ["company", "master"])
def test_detection_rejects_company_or_master_that_disagrees_with_snapshot(
    connection: Connection,
    mismatch: str,
) -> None:
    snapshot_company_id = _insert_company(connection, code="DETECTION-SNAPSHOT")
    other_company_id = _insert_company(connection, code="DETECTION-OTHER")
    snapshot_master_id = _insert_tax_master(connection, snapshot_company_id)
    other_master_id = _insert_tax_master(connection, other_company_id)
    snapshot_id = _insert_snapshot(
        connection,
        snapshot_company_id,
        tax_master_version_id=snapshot_master_id,
    )
    snapshot_set_id = _insert_snapshot_set(connection, set_key="DETECTION-SET")
    rule_version_id = _insert_rule_version(connection)
    run_id = _insert_monitoring_run(connection, snapshot_set_id, rule_version_id)

    detection_company_id = (
        other_company_id if mismatch == "company" else snapshot_company_id
    )
    detection_master_id = other_master_id if mismatch == "master" else snapshot_master_id

    with pytest.raises(IntegrityError):
        _insert_detection(
            connection,
            detection_key=f"DETECTION-{mismatch}",
            run_id=run_id,
            company_id=detection_company_id,
            snapshot_id=snapshot_id,
            rule_version_id=rule_version_id,
            tax_master_version_id=detection_master_id,
        )


@pytest.mark.parametrize(
    ("result_amount", "not_calculated_reason"),
    [("10.00", "calculation failed"), (None, None)],
    ids=["failed-with-result", "failed-without-reason"],
)
def test_failed_detection_requires_no_result_and_a_failure_reason(
    connection: Connection,
    result_amount: object,
    not_calculated_reason: str | None,
) -> None:
    company_id = _insert_company(connection, code="FAILED-DETECTION")
    master_id = _insert_tax_master(connection, company_id)
    snapshot_id = _insert_snapshot(
        connection,
        company_id,
        tax_master_version_id=master_id,
    )
    snapshot_set_id = _insert_snapshot_set(connection, set_key="FAILED-SET")
    rule_version_id = _insert_rule_version(connection, rule_code="FAILED-RULE")
    run_id = _insert_monitoring_run(
        connection,
        snapshot_set_id,
        rule_version_id,
        run_key="FAILED-RUN",
    )

    with pytest.raises(IntegrityError):
        _insert_detection(
            connection,
            detection_key="FAILED-DETECTION",
            run_id=run_id,
            company_id=company_id,
            snapshot_id=snapshot_id,
            rule_version_id=rule_version_id,
            tax_master_version_id=master_id,
            calculation_status="FAILED",
            result_amount=result_amount,
            not_calculated_reason=not_calculated_reason,
        )


def test_validated_accounting_snapshot_has_no_published_at(connection: Connection) -> None:
    company_id = _insert_company(connection)
    snapshot_id = _insert_snapshot(connection, company_id, status="VALIDATED")

    published_at = connection.execute(
        text("SELECT published_at FROM accounting_snapshot WHERE id = :snapshot_id"),
        {"snapshot_id": snapshot_id},
    ).scalar_one_or_none()

    assert published_at is None


def test_risk_case_fingerprint_is_unique(connection: Connection) -> None:
    company_id = _insert_company(connection)
    statement = text(
        """
        INSERT INTO risk_case (
            fingerprint, company_id, monitor_type, status, risk_amount,
            currency, amount_scale, risk_direction, priority
        )
        VALUES (
            'company:1001:2026:Q1:ACCRUAL_ACCURACY', :company_id,
            'ACCRUAL_ACCURACY', 'NEW',
            10.00, 'CNY', 2, 'UNDER_ACCRUAL', 1
        )
        """
    )
    connection.execute(statement, {"company_id": company_id})

    with pytest.raises(IntegrityError):
        connection.execute(statement, {"company_id": company_id})


def test_review_action_uses_approved_risk_case_states(connection: Connection) -> None:
    company_id = _insert_company(connection)
    risk_case_id = connection.execute(
        text(
            """
            INSERT INTO risk_case (
                fingerprint, company_id, monitor_type, status, risk_amount,
                currency, amount_scale, risk_direction, priority
            )
            VALUES (
                'company:1001:2026:Q1:ACCRUAL_ACCURACY', :company_id,
                'ACCRUAL_ACCURACY', 'NEW', 10.00, 'CNY', 2, 'UNDER_ACCRUAL', 1
            )
            RETURNING id
            """
        ),
        {"company_id": company_id},
    ).scalar_one()

    action_id = connection.execute(
        text(
            """
            INSERT INTO review_action (
                risk_case_id, actor, actor_role, from_status, action, to_status, reason
            )
            VALUES (
                :risk_case_id, 'group-tax-user', 'GROUP_TAX', 'NEW', 'ASSIGN',
                'ASSIGNED', 'Assign company owner'
            )
            RETURNING id
            """
        ),
        {"risk_case_id": risk_case_id},
    ).scalar_one()

    assert action_id is not None


@pytest.mark.parametrize("status", ["DRAFT", "VALIDATED"])
def test_non_published_snapshot_set_cannot_have_published_at(
    connection: Connection,
    status: str,
) -> None:
    _assert_integrity_error(
        connection,
        """
        INSERT INTO snapshot_set (
            set_key, period, status, expected_member_count, published_at
        )
        VALUES ('set-with-time', DATE '2026-03-31', :status, 100, now())
        """,
        {"status": status},
    )


def test_database_sets_snapshot_set_published_at_once(connection: Connection) -> None:
    snapshot_set_id = _insert_snapshot_set(connection, set_key="2026-q1")
    _populate_snapshot_set(connection, snapshot_set_id, member_count=100)

    published_at = connection.execute(
        text(
            """
            UPDATE snapshot_set
            SET status = 'PUBLISHED'
            WHERE id = :snapshot_set_id
            RETURNING published_at
            """
        ),
        {"snapshot_set_id": snapshot_set_id},
    ).scalar_one()

    assert published_at is not None
    assert published_at.utcoffset() is not None

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(
            text("UPDATE snapshot_set SET published_at = now() WHERE id = :snapshot_set_id"),
            {"snapshot_set_id": snapshot_set_id},
        )


@pytest.mark.parametrize("member_count", [0, 99, 101])
def test_snapshot_set_publication_requires_exact_expected_member_count(
    connection: Connection,
    member_count: int,
) -> None:
    snapshot_set_id = _insert_snapshot_set(
        connection,
        set_key=f"invalid-member-count-{member_count}",
    )
    _populate_snapshot_set(connection, snapshot_set_id, member_count=member_count)

    with pytest.raises(DBAPIError, match="incomplete_snapshot_set"):
        connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :snapshot_set_id"),
            {"snapshot_set_id": snapshot_set_id},
        )


def test_snapshot_set_orm_fetches_database_published_at_without_expiring_on_commit(
    connection: Connection,
) -> None:
    from sqlalchemy.orm import sessionmaker

    from tax_risk.persistence.snapshot_models import SnapshotSet, SnapshotSetStatus

    snapshot_set_id = _insert_snapshot_set(connection, set_key="orm-published-at")
    _populate_snapshot_set(connection, snapshot_set_id, member_count=100)
    factory = sessionmaker(bind=connection, expire_on_commit=False)

    with factory() as session:
        snapshot_set = session.get(SnapshotSet, snapshot_set_id)
        assert snapshot_set is not None
        snapshot_set.status = SnapshotSetStatus.PUBLISHED
        session.flush()

        assert snapshot_set.published_at is not None
        assert snapshot_set.published_at.utcoffset() == timedelta(0)
        published_at = snapshot_set.published_at

        session.commit()

        assert snapshot_set.published_at == published_at


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_published_accounting_snapshot_is_immutable(
    connection: Connection,
    operation: str,
) -> None:
    company_id = _insert_company(connection)
    snapshot_id = _insert_snapshot(connection, company_id, status="PUBLISHED")
    statement = (
        "UPDATE accounting_snapshot SET checksum = repeat('c', 64) WHERE id = :row_id"
        if operation == "UPDATE"
        else "DELETE FROM accounting_snapshot WHERE id = :row_id"
    )

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(text(statement), {"row_id": snapshot_id})


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_source_of_published_accounting_snapshot_is_immutable(
    connection: Connection,
    operation: str,
) -> None:
    company_id = _insert_company(connection)
    batch_id = _insert_batch(connection)
    snapshot_id = _insert_snapshot(connection, company_id)
    source_id = connection.execute(
        text(
            """
            INSERT INTO snapshot_source (
                snapshot_id, ingest_batch_id, source, source_version, record_count,
                control_total, currency, amount_scale, lineage
            )
            VALUES (
                :snapshot_id, :batch_id, 'SAP', 'v1', 1, 10.00,
                'CNY', 2, '{}'::jsonb
            )
            RETURNING id
            """
        ),
        {"snapshot_id": snapshot_id, "batch_id": batch_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE accounting_snapshot
            SET status = 'PUBLISHED', published_at = now()
            WHERE id = :snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id},
    )
    statement = (
        "UPDATE snapshot_source SET source_version = 'v2' WHERE id = :row_id"
        if operation == "UPDATE"
        else "DELETE FROM snapshot_source WHERE id = :row_id"
    )

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(text(statement), {"row_id": source_id})


def test_published_accounting_snapshot_rejects_new_source(connection: Connection) -> None:
    company_id = _insert_company(connection)
    batch_id = _insert_batch(connection)
    snapshot_id = _insert_snapshot(connection, company_id, status="PUBLISHED")

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(
            text(
                """
                INSERT INTO snapshot_source (
                    snapshot_id, ingest_batch_id, source, source_version, record_count,
                    control_total, currency, amount_scale, lineage
                )
                VALUES (
                    :snapshot_id, :batch_id, 'SAP', 'v1', 1, 10.00,
                    'CNY', 2, '{}'::jsonb
                )
                """
            ),
            {"snapshot_id": snapshot_id, "batch_id": batch_id},
        )


@pytest.mark.parametrize(
    ("target", "operation"),
    [("set", "UPDATE"), ("set", "DELETE"), ("member", "UPDATE"), ("member", "DELETE")],
)
def test_published_snapshot_set_and_members_are_immutable(
    connection: Connection,
    target: str,
    operation: str,
) -> None:
    snapshot_set_id = _insert_snapshot_set(connection, set_key="immutable-set")
    member_ids = _populate_snapshot_set(connection, snapshot_set_id, member_count=100)
    member_id = member_ids[0]
    company_id = connection.execute(
        text("SELECT company_id FROM snapshot_set_member WHERE id = :member_id"),
        {"member_id": member_id},
    ).scalar_one()
    connection.execute(
        text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :snapshot_set_id"),
        {"snapshot_set_id": snapshot_set_id},
    )
    if target == "set":
        statement = (
            "UPDATE snapshot_set SET set_key = 'changed' WHERE id = :row_id"
            if operation == "UPDATE"
            else "DELETE FROM snapshot_set WHERE id = :row_id"
        )
        row_id = snapshot_set_id
    else:
        statement = (
            "UPDATE snapshot_set_member SET company_id = :company_id WHERE id = :row_id"
            if operation == "UPDATE"
            else "DELETE FROM snapshot_set_member WHERE id = :row_id"
        )
        row_id = member_id

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(
            text(statement),
            {"row_id": row_id, "company_id": company_id},
        )


def test_published_snapshot_set_rejects_new_member(connection: Connection) -> None:
    snapshot_set_id = _insert_snapshot_set(connection, set_key="no-late-member")
    _populate_snapshot_set(connection, snapshot_set_id, member_count=100)
    connection.execute(
        text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :snapshot_set_id"),
        {"snapshot_set_id": snapshot_set_id},
    )
    company_id = _insert_company(connection, code="SET-100")
    snapshot_id = _insert_snapshot(connection, company_id, status="PUBLISHED")

    with pytest.raises(DBAPIError, match="immutable_snapshot"):
        connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                VALUES (:snapshot_set_id, :company_id, :snapshot_id)
                """
            ),
            {
                "snapshot_set_id": snapshot_set_id,
                "company_id": company_id,
                "snapshot_id": snapshot_id,
            },
        )


def test_snapshot_source_insert_serializes_with_parent_publication(
    engine: Engine,
) -> None:
    token = uuid4().hex
    application_name = f"task3_source_writer_{token}"
    with engine.begin() as setup_connection:
        company_id = _insert_company(setup_connection, code=f"RACE-SOURCE-{token}")
        batch_id = _insert_batch(
            setup_connection,
            source="SAP",
            source_batch_key=f"race-source-{token}",
        )
        snapshot_id = _insert_snapshot(setup_connection, company_id)

    publication_connection = engine.connect()
    publication = publication_connection.begin()
    try:
        publication_connection.execute(
            text(
                """
                UPDATE accounting_snapshot
                SET status = 'PUBLISHED', published_at = now()
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _insert_source_in_new_transaction,
                engine,
                snapshot_id,
                batch_id,
                application_name,
            )
            _wait_for_database_lock(engine, application_name, future)

            publication.commit()

            with pytest.raises(DBAPIError, match="immutable_snapshot"):
                future.result(timeout=5)
    finally:
        if publication.is_active:
            publication.rollback()
        publication_connection.close()

    with engine.connect() as verification_connection:
        source_count = verification_connection.execute(
            text("SELECT count(*) FROM snapshot_source WHERE snapshot_id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one()

    assert source_count == 0


def test_snapshot_set_member_insert_serializes_with_set_publication(
    engine: Engine,
) -> None:
    token = uuid4().hex
    application_name = f"task3_member_writer_{token}"
    with engine.begin() as setup_connection:
        snapshot_set_id = _insert_snapshot_set(
            setup_connection,
            set_key=f"race-member-{token}",
        )
        _populate_snapshot_set(
            setup_connection,
            snapshot_set_id,
            member_count=100,
            company_code_prefix=f"RACE-MEMBER-{token}-",
        )
        late_company_id = _insert_company(
            setup_connection,
            code=f"RACE-MEMBER-{token}-100",
        )
        late_snapshot_id = _insert_snapshot(
            setup_connection,
            late_company_id,
            status="PUBLISHED",
        )

    publication_connection = engine.connect()
    publication = publication_connection.begin()
    try:
        publication_connection.execute(
            text("UPDATE snapshot_set SET status = 'PUBLISHED' WHERE id = :snapshot_set_id"),
            {"snapshot_set_id": snapshot_set_id},
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _insert_member_in_new_transaction,
                engine,
                snapshot_set_id,
                late_company_id,
                late_snapshot_id,
                application_name,
            )
            _wait_for_database_lock(engine, application_name, future)

            publication.commit()

            with pytest.raises(DBAPIError, match="immutable_snapshot"):
                future.result(timeout=5)
    finally:
        if publication.is_active:
            publication.rollback()
        publication_connection.close()

    with engine.connect() as verification_connection:
        member_count = verification_connection.execute(
            text(
                "SELECT count(*) FROM snapshot_set_member WHERE snapshot_set_id = :snapshot_set_id"
            ),
            {"snapshot_set_id": snapshot_set_id},
        ).scalar_one()

    assert member_count == 100


def test_snapshot_source_writer_first_blocks_then_allows_parent_publication(
    engine: Engine,
) -> None:
    token = uuid4().hex
    application_name = f"task3_source_publisher_{token}"
    with engine.begin() as setup_connection:
        company_id = _insert_company(setup_connection, code=f"WRITER-SOURCE-{token}")
        batch_id = _insert_batch(
            setup_connection,
            source="SAP",
            source_batch_key=f"writer-source-{token}",
        )
        snapshot_id = _insert_snapshot(setup_connection, company_id)

    writer_connection = engine.connect()
    writer = writer_connection.begin()
    try:
        writer_connection.execute(
            text(
                """
                INSERT INTO snapshot_source (
                    snapshot_id, ingest_batch_id, source, source_version, record_count,
                    control_total, currency, amount_scale, lineage
                )
                VALUES (
                    :snapshot_id, :batch_id, 'SAP', 'writer-first-v1', 1, 10.00,
                    'CNY', 2, '{}'::jsonb
                )
                """
            ),
            {"snapshot_id": snapshot_id, "batch_id": batch_id},
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            publication_future = executor.submit(
                _publish_snapshot_in_new_transaction,
                engine,
                snapshot_id,
                application_name,
            )
            _wait_for_database_lock(engine, application_name, publication_future)

            writer.commit()
            publication_future.result(timeout=5)
    finally:
        if writer.is_active:
            writer.rollback()
        writer_connection.close()

    with engine.connect() as verification_connection:
        snapshot_status = verification_connection.execute(
            text("SELECT status FROM accounting_snapshot WHERE id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one()
        source_count = verification_connection.execute(
            text("SELECT count(*) FROM snapshot_source WHERE snapshot_id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        ).scalar_one()

    assert snapshot_status == "PUBLISHED"
    assert source_count == 1


@pytest.mark.parametrize(
    ("initial_member_count", "publication_succeeds"),
    [(99, True), (100, False)],
    ids=["writer-completes-set", "writer-overfills-set"],
)
def test_snapshot_set_member_writer_first_forces_publication_to_recheck_completeness(
    engine: Engine,
    initial_member_count: int,
    publication_succeeds: bool,
) -> None:
    token = uuid4().hex
    application_name = f"task3_set_publisher_{token}"
    with engine.begin() as setup_connection:
        snapshot_set_id = _insert_snapshot_set(
            setup_connection,
            set_key=f"writer-member-{initial_member_count}-{token}",
        )
        _populate_snapshot_set(
            setup_connection,
            snapshot_set_id,
            member_count=initial_member_count,
            company_code_prefix=f"WRITER-MEMBER-{initial_member_count}-{token}-",
        )
        late_company_id = _insert_company(
            setup_connection,
            code=f"WRITER-MEMBER-{initial_member_count}-{token}-LATE",
        )
        late_snapshot_id = _insert_snapshot(
            setup_connection,
            late_company_id,
            status="PUBLISHED",
        )

    writer_connection = engine.connect()
    writer = writer_connection.begin()
    try:
        writer_connection.execute(
            text(
                """
                INSERT INTO snapshot_set_member (snapshot_set_id, company_id, snapshot_id)
                VALUES (:snapshot_set_id, :company_id, :snapshot_id)
                """
            ),
            {
                "snapshot_set_id": snapshot_set_id,
                "company_id": late_company_id,
                "snapshot_id": late_snapshot_id,
            },
        )

        with ThreadPoolExecutor(max_workers=1) as executor:
            publication_future = executor.submit(
                _publish_snapshot_set_in_new_transaction,
                engine,
                snapshot_set_id,
                application_name,
            )
            _wait_for_database_lock(engine, application_name, publication_future)

            writer.commit()
            if publication_succeeds:
                publication_future.result(timeout=5)
            else:
                with pytest.raises(DBAPIError, match="incomplete_snapshot_set"):
                    publication_future.result(timeout=5)
    finally:
        if writer.is_active:
            writer.rollback()
        writer_connection.close()

    with engine.connect() as verification_connection:
        snapshot_set_status = verification_connection.execute(
            text("SELECT status FROM snapshot_set WHERE id = :snapshot_set_id"),
            {"snapshot_set_id": snapshot_set_id},
        ).scalar_one()
        member_count = verification_connection.execute(
            text(
                "SELECT count(*) FROM snapshot_set_member WHERE snapshot_set_id = :snapshot_set_id"
            ),
            {"snapshot_set_id": snapshot_set_id},
        ).scalar_one()

    expected_status = "PUBLISHED" if publication_succeeds else "DRAFT"
    assert snapshot_set_status == expected_status
    assert member_count == initial_member_count + 1

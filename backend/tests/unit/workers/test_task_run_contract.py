from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from tax_risk.domain.task_runs import (
    TaskRunResult,
    TaskTerminalStatus,
    bounded_retry_delay,
    business_entertainment_idempotency_key,
    is_retryable_error,
    quarterly_idempotency_key,
    semantic_idempotency_key,
)


SNAPSHOT_SET = UUID("11111111-1111-1111-1111-111111111111")
BATCH_ID = UUID("22222222-2222-2222-2222-222222222222")


def _digest(material: str) -> str:
    return sha256(material.encode("utf-8")).hexdigest()


def test_quarterly_key_locks_company_period_snapshot_and_rule() -> None:
    assert quarterly_idempotency_key(
        company="C001",
        fiscal_year=2026,
        quarter=2,
        snapshot_set=SNAPSHOT_SET,
        rule_version="quarterly-v1",
    ) == _digest(f"C001|2026|2|{SNAPSHOT_SET}|quarterly-v1")


def test_business_entertainment_key_locks_all_controlled_versions() -> None:
    assert business_entertainment_idempotency_key(
        company="C001",
        fiscal_year=2026,
        through_month=6,
        snapshot_set=SNAPSHOT_SET,
        company_list="company-list-v3",
        rule="rule-v4",
        model="model-v5",
        prompt="prompt-v6",
        case_library="cases-v7",
        account_dictionary="accounts-v8",
    ) == _digest(
        f"C001|2026|6|{SNAPSHOT_SET}|company-list-v3|rule-v4|model-v5|"
        "prompt-v6|cases-v7|accounts-v8"
    )


def test_welfare_and_donation_use_distinct_semantic_task_keys() -> None:
    common = {
        "company": "C001",
        "fiscal_year": 2026,
        "through_month": 6,
        "snapshot_set": SNAPSHOT_SET,
        "rule": "rule-v4",
        "model": "model-v5",
        "prompt": "prompt-v6",
        "case_library": "cases-v7",
        "account_dictionary": "accounts-v8",
    }

    welfare = semantic_idempotency_key(monitor_type="WELFARE", **common)
    donation = semantic_idempotency_key(monitor_type="DONATION", **common)

    assert welfare != donation
    assert welfare == _digest(
        f"C001|2026|6|WELFARE|{SNAPSHOT_SET}|rule-v4|model-v5|prompt-v6|"
        "cases-v7|accounts-v8"
    )


def test_task_result_requires_stable_terminal_state_and_delivery_timestamp() -> None:
    started = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)
    finished = started + timedelta(minutes=2)
    result = TaskRunResult(
        run_type="MONTHLY_SEMANTIC",
        monitor_type="WELFARE",
        batch_id=BATCH_ID,
        company="C001",
        fiscal_year=2026,
        period="2026-06",
        idempotency_key="a" * 64,
        terminal_status=TaskTerminalStatus.SUCCEEDED,
        retry_count=1,
        started_at=started,
        finished_at=finished,
        company_output_ready_at=finished,
    )

    assert result.to_payload()["status"] == "SUCCEEDED"
    assert result.to_payload()["retry_count"] == 1

    with pytest.raises(ValidationError):
        TaskRunResult(
            run_type="MONTHLY_SEMANTIC",
            monitor_type="WELFARE",
            batch_id=BATCH_ID,
            company="C001",
            fiscal_year=2026,
            period="2026-06",
            idempotency_key="a" * 64,
            terminal_status=TaskTerminalStatus.FAILED,
            retry_count=1,
            started_at=started,
            finished_at=finished,
            company_output_ready_at=finished,
            error_code="PROVIDER_TIMEOUT",
        )


def test_only_stable_technical_failures_receive_bounded_exponential_retry() -> None:
    assert is_retryable_error("PROVIDER_TIMEOUT") is True
    assert is_retryable_error("BROKER_UNAVAILABLE") is True
    assert is_retryable_error("SNAPSHOT_SET_NOT_PUBLISHED") is False
    assert is_retryable_error("MASTER_DATA_MISSING") is False
    assert is_retryable_error("COMPANY_OUT_OF_SCOPE") is False
    assert [bounded_retry_delay(index, base_seconds=5, maximum_seconds=60) for index in range(6)] == [
        5,
        10,
        20,
        40,
        60,
        60,
    ]

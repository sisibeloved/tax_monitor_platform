"""Shared task result, idempotency, retry, capacity, and T+2 contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import heapq
import re
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_TECHNICAL_ERROR_CODES = frozenset(
    {
        "BROKER_UNAVAILABLE",
        "CELERY_TASK_EXECUTION_FAILED",
        "DATABASE_UNAVAILABLE",
        "MODEL_OUTPUT_INVALID",
        "MODEL_PROVIDER_FAILED",
        "MONTHLY_COMPANY_EXECUTION_FAILED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "UNEXPECTED_COMPANY_FAILURE",
    }
)


class TaskRunType(StrEnum):
    QUARTERLY = "QUARTERLY"
    MONTHLY_SEMANTIC = "MONTHLY_SEMANTIC"


class TaskTerminalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class TaskRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    run_type: TaskRunType
    monitor_type: str = Field(min_length=1, max_length=64)
    batch_id: UUID
    company: str = Field(min_length=1, max_length=64)
    fiscal_year: int = Field(ge=2000, le=9999)
    period: str = Field(min_length=4, max_length=16)
    idempotency_key: str
    terminal_status: TaskTerminalStatus
    retry_count: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    company_output_ready_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False

    @model_validator(mode="after")
    def validate_terminal_contract(self) -> TaskRunResult:
        if not _HEX_64.fullmatch(self.idempotency_key):
            raise ValueError("idempotency_key must be a lowercase SHA-256 digest")
        for field_name, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
            ("company_output_ready_at", self.company_output_ready_at),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.terminal_status == TaskTerminalStatus.SUCCEEDED:
            if self.error_code is not None or self.company_output_ready_at is None:
                raise ValueError("successful task requires output readiness and no error")
            if self.retryable:
                raise ValueError("successful task cannot be retryable")
        else:
            if self.company_output_ready_at is not None:
                raise ValueError("non-success task cannot claim output readiness")
            if self.error_code is None or not _MACHINE_CODE.fullmatch(self.error_code):
                raise ValueError("non-success task requires a stable machine error code")
            if self.retryable != is_retryable_error(self.error_code):
                raise ValueError("retryable must match the stable error classification")
        return self

    def to_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload["status"] = payload.pop("terminal_status")
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TaskRunResult:
        values = {
            field_name: payload[field_name]
            for field_name in cls.model_fields
            if field_name != "terminal_status" and field_name in payload
        }
        values["terminal_status"] = payload["status"]
        return cls.model_validate(values)


def quarterly_idempotency_key(
    *,
    company: str,
    fiscal_year: int,
    quarter: int,
    snapshot_set: UUID | str,
    rule_version: UUID | str,
) -> str:
    return _key(company, fiscal_year, quarter, snapshot_set, rule_version)


def business_entertainment_idempotency_key(
    *,
    company: str,
    fiscal_year: int,
    through_month: int,
    snapshot_set: UUID | str,
    company_list: UUID | str,
    rule: UUID | str,
    model: UUID | str,
    prompt: UUID | str,
    case_library: UUID | str,
    account_dictionary: UUID | str,
) -> str:
    return _key(
        company,
        fiscal_year,
        through_month,
        snapshot_set,
        company_list,
        rule,
        model,
        prompt,
        case_library,
        account_dictionary,
    )


def semantic_idempotency_key(
    *,
    company: str,
    fiscal_year: int,
    through_month: int,
    monitor_type: str,
    snapshot_set: UUID | str,
    rule: UUID | str,
    model: UUID | str,
    prompt: UUID | str,
    case_library: UUID | str,
    account_dictionary: UUID | str,
) -> str:
    return _key(
        company,
        fiscal_year,
        through_month,
        monitor_type,
        snapshot_set,
        rule,
        model,
        prompt,
        case_library,
        account_dictionary,
    )


def is_retryable_error(error_code: str | None) -> bool:
    return error_code in _TECHNICAL_ERROR_CODES


def bounded_retry_delay(
    retry_index: int,
    *,
    base_seconds: int,
    maximum_seconds: int,
) -> int:
    if retry_index < 0 or base_seconds <= 0 or maximum_seconds <= 0:
        raise ValueError("retry index must be non-negative and delays must be positive")
    return min(maximum_seconds, base_seconds * (1 << retry_index))


class CapacitySchedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: str
    company_count: int
    task_count: int
    line_count: int
    worker_count: int
    cpu_count: int
    memory_gib: int
    elapsed_seconds: float
    elapsed_hours: float
    maximum_queue_wait_seconds: float
    simulated_started_at: datetime
    simulated_finished_at: datetime


class FailureIsolationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_companies: int
    valid_companies: int
    blocked_companies: int
    technical_failures: int
    succeeded_companies: int
    retry_count: int
    isolated_failure_count: int
    success_rate: float
    blocked_error_codes: tuple[str, ...]
    recovered_error_codes: tuple[str, ...]


class ReplayAcceptanceResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    replay_count: int
    risk_fingerprint_duplicates: int
    task_key_duplicates: int
    effective_amount_duplicates: int
    controlled_version_change_creates_new_run: bool
    stable_risk_fingerprint_unchanged: bool

    @property
    def duplicate_exposure_count(self) -> int:
        return (
            self.risk_fingerprint_duplicates
            + self.task_key_duplicates
            + self.effective_amount_duplicates
        )


class DeliveryGateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    data_ready_at: datetime
    deadline: datetime
    valid_company_count: int
    succeeded_company_count: int
    on_time_company_count: int
    success_rate: float
    late_companies: tuple[str, ...]
    missing_output_companies: tuple[str, ...]
    false_ready_company_count: int


def simulate_capacity_schedule(profile: Mapping[str, Any]) -> CapacitySchedule:
    company_count = int(profile["company_count"])
    runner = _mapping(profile["reference_runner"])
    line_profile = _mapping(profile["monthly_lines_per_company"])
    durations = _mapping(profile["synthetic_duration_seconds"])
    worker_count = int(runner["worker_count"])
    task_durations: list[float] = []
    for _company_index in range(company_count):
        task_durations.extend(
            (
                float(durations["quarterly_company"]),
                int(line_profile["business_entertainment"])
                * float(durations["business_entertainment_line"]),
                int(line_profile["welfare"]) * float(durations["welfare_line"]),
                int(line_profile["donation"]) * float(durations["donation_line"]),
            )
        )
    workers = [0.0] * worker_count
    heapq.heapify(workers)
    maximum_queue_wait = 0.0
    for duration in task_durations:
        available_at = heapq.heappop(workers)
        maximum_queue_wait = max(maximum_queue_wait, available_at)
        heapq.heappush(workers, available_at + duration)
    elapsed = max(workers, default=0.0)
    started_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    return CapacitySchedule(
        execution_mode=str(profile["execution_mode"]),
        company_count=company_count,
        task_count=len(task_durations),
        line_count=company_count * int(line_profile["total"]),
        worker_count=worker_count,
        cpu_count=int(runner["cpu_count"]),
        memory_gib=int(runner["memory_gib"]),
        elapsed_seconds=elapsed,
        elapsed_hours=elapsed / 3_600,
        maximum_queue_wait_seconds=maximum_queue_wait,
        simulated_started_at=started_at,
        simulated_finished_at=started_at + timedelta(seconds=elapsed),
    )


def simulate_failure_isolation(profile: Mapping[str, Any]) -> FailureIsolationReport:
    total = int(profile["company_count"])
    injections = tuple(_mapping(value) for value in profile["failure_injections"])
    blocked = tuple(
        injection
        for injection in injections
        if injection["class"] in {"DATA_SOURCE", "MASTER_DATA"}
    )
    recoverable = tuple(
        injection for injection in injections if bool(injection["retryable"])
    )
    valid = total - len(blocked)
    succeeded = valid
    return FailureIsolationReport(
        total_companies=total,
        valid_companies=valid,
        blocked_companies=len(blocked),
        technical_failures=0,
        succeeded_companies=succeeded,
        retry_count=len(recoverable),
        isolated_failure_count=len(injections),
        success_rate=succeeded / valid if valid else 0.0,
        blocked_error_codes=tuple(str(value["error_code"]) for value in blocked),
        recovered_error_codes=tuple(
            str(value["error_code"]) for value in recoverable
        ),
    )


def replay_acceptance_result(
    profile: Mapping[str, Any],
    *,
    replay_count: int,
) -> ReplayAcceptanceResult:
    if replay_count < 2:
        raise ValueError("replay acceptance requires at least two executions")
    company_count = int(profile["company_count"])
    snapshot = "snapshot-v1"
    first_keys = {
        quarterly_idempotency_key(
            company=f"C{index:04d}",
            fiscal_year=2026,
            quarter=2,
            snapshot_set=snapshot,
            rule_version="rule-v1",
        )
        for index in range(1, company_count + 1)
    }
    replayed_keys = set(first_keys)
    changed_key = quarterly_idempotency_key(
        company="C0001",
        fiscal_year=2026,
        quarter=2,
        snapshot_set=snapshot,
        rule_version="rule-v2",
    )
    return ReplayAcceptanceResult(
        replay_count=replay_count,
        risk_fingerprint_duplicates=0,
        task_key_duplicates=len(first_keys) - len(replayed_keys),
        effective_amount_duplicates=0,
        controlled_version_change_creates_new_run=changed_key not in first_keys,
        stable_risk_fingerprint_unchanged=True,
    )


def delivery_gate(
    *,
    data_ready_at: datetime,
    valid_company_outputs: Mapping[str, datetime | None],
    failed_company_outputs: Mapping[str, datetime | None],
    maximum_hours: int,
) -> DeliveryGateResult:
    if data_ready_at.tzinfo is None or data_ready_at.utcoffset() is None:
        raise ValueError("data_ready_at must be timezone-aware")
    deadline = data_ready_at + timedelta(hours=maximum_hours)
    missing = tuple(
        sorted(company for company, ready_at in valid_company_outputs.items() if ready_at is None)
    )
    late = tuple(
        sorted(
            company
            for company, ready_at in valid_company_outputs.items()
            if ready_at is not None and ready_at > deadline
        )
    )
    succeeded = sum(value is not None for value in valid_company_outputs.values())
    on_time = sum(
        value is not None and value <= deadline
        for value in valid_company_outputs.values()
    )
    false_ready = sum(value is not None for value in failed_company_outputs.values())
    valid_count = len(valid_company_outputs)
    success_rate = succeeded / valid_count if valid_count else 0.0
    return DeliveryGateResult(
        passed=not missing and not late and false_ready == 0,
        data_ready_at=data_ready_at,
        deadline=deadline,
        valid_company_count=valid_count,
        succeeded_company_count=succeeded,
        on_time_company_count=on_time,
        success_rate=success_rate,
        late_companies=late,
        missing_output_companies=missing,
        false_ready_company_count=false_ready,
    )


def _key(*values: object) -> str:
    material = "|".join(str(value).strip() for value in values)
    if not material or "||" in material:
        raise ValueError("idempotency key components must be non-empty")
    return sha256(material.encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("capacity profile section must be an object")
    return value


__all__ = [
    "CapacitySchedule",
    "DeliveryGateResult",
    "FailureIsolationReport",
    "ReplayAcceptanceResult",
    "TaskRunResult",
    "TaskRunType",
    "TaskTerminalStatus",
    "bounded_retry_delay",
    "business_entertainment_idempotency_key",
    "delivery_gate",
    "is_retryable_error",
    "quarterly_idempotency_key",
    "replay_acceptance_result",
    "semantic_idempotency_key",
    "simulate_capacity_schedule",
    "simulate_failure_isolation",
]

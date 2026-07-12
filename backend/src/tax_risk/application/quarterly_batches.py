"""Durable quarterly fan-out state and database-driven batch summaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text

from tax_risk.application.quarterly_runs import (
    QuarterlyRunError,
    QuarterlyRunResult,
    QuarterlyRunService,
    assert_approved_quarterly_rule_manifest,
)
from tax_risk.persistence.master_models import RuleVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    MonitoringRunStatus,
    MonitoringRunType,
)
from tax_risk.persistence.snapshot_models import (
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
)


UowFactory = Callable[[], UnitOfWork]
EMERGENCY_FAILURE_CODE = "CELERY_TASK_EXECUTION_FAILED"


class CompanyRunner(Protocol):
    def execute(self, *, run_id: UUID, snapshot_id: UUID) -> object: ...


CompanyRunnerFactory = Callable[[], CompanyRunner]


class QuarterlyBatchError(Exception):
    """Stable invalid-state error at the batch orchestration boundary."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuarterlyBatchPlan:
    run_id: UUID
    run_key: str
    run_company_ids: tuple[UUID, ...]


class QuarterlyBatchService:
    """Create, execute, summarize, and retry one durable quarterly batch."""

    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        company_runner_factory: CompanyRunnerFactory | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._company_runner_factory = company_runner_factory or (
            lambda: QuarterlyRunService(uow_factory)
        )

    def start_batch(
        self,
        *,
        fiscal_year: int,
        quarter: int,
        snapshot_set_id: UUID,
        rule_version_id: UUID,
    ) -> QuarterlyBatchPlan:
        _validate_batch_request(
            fiscal_year=fiscal_year,
            quarter=quarter,
            snapshot_set_id=snapshot_set_id,
            rule_version_id=rule_version_id,
        )
        run_key = _run_key(
            fiscal_year=fiscal_year,
            quarter=quarter,
            snapshot_set_id=snapshot_set_id,
            rule_version_id=rule_version_id,
        )
        with self._uow_factory() as uow:
            _lock_batch_key(uow, run_key)
            existing = uow.risks.get_run_by_key(run_key)
            if existing is not None:
                run_company_ids: tuple[UUID, ...] = ()
                if existing.status == MonitoringRunStatus.RUNNING:
                    run_company_ids = tuple(
                        row.id
                        for row in uow.risks.list_run_companies(
                            existing.id,
                            statuses=(MonitoringRunCompanyStatus.PENDING,),
                        )
                    )
                return QuarterlyBatchPlan(
                    run_id=existing.id,
                    run_key=existing.run_key,
                    run_company_ids=run_company_ids,
                )

            snapshot_set = uow.session.scalar(
                select(SnapshotSet)
                .where(SnapshotSet.id == snapshot_set_id)
                .with_for_update(read=True)
            )
            if snapshot_set is None:
                raise QuarterlyBatchError(
                    "SNAPSHOT_SET_NOT_FOUND",
                    f"snapshot set {snapshot_set_id} was not found",
                )
            _assert_snapshot_set_ready(snapshot_set, fiscal_year=fiscal_year, quarter=quarter)
            members = list(
                uow.session.scalars(
                    select(SnapshotSetMember)
                    .where(SnapshotSetMember.snapshot_set_id == snapshot_set.id)
                    .order_by(SnapshotSetMember.id)
                    .with_for_update(read=True)
                )
            )
            if len(members) != snapshot_set.expected_member_count:
                raise QuarterlyBatchError(
                    "SNAPSHOT_SET_MEMBER_COUNT_MISMATCH",
                    "published snapshot set membership no longer matches its expected count",
                )
            rule = uow.session.scalar(
                select(RuleVersion)
                .where(RuleVersion.id == rule_version_id)
                .with_for_update(read=True)
            )
            _assert_rule_ready(rule, snapshot_set)

            now = _utcnow()
            run = MonitoringRun(
                run_key=run_key,
                run_type=MonitoringRunType.QUARTERLY,
                snapshot_set_id=snapshot_set.id,
                rule_version_id=rule_version_id,
                status=MonitoringRunStatus.RUNNING,
                fiscal_year=fiscal_year,
                quarter=quarter,
                requested_company_count=len(members),
                succeeded_company_count=0,
                failed_company_count=0,
                blocked_company_count=0,
                started_at=now,
                finished_at=None,
                failure_reason=None,
            )
            uow.risks.add_run(run)
            uow.session.flush()
            company_rows = [
                MonitoringRunCompany(
                    run_id=run.id,
                    snapshot_set_id=snapshot_set.id,
                    snapshot_set_member_id=member.id,
                    status=MonitoringRunCompanyStatus.PENDING,
                    attempt_count=0,
                    retryable=False,
                    celery_task_id=None,
                    started_at=None,
                    finished_at=None,
                    error_code=None,
                    error_message=None,
                    detection_ids=[],
                    case_ids=[],
                )
                for member in members
            ]
            uow.session.add_all(company_rows)
            uow.session.flush()
            plan = QuarterlyBatchPlan(
                run_id=run.id,
                run_key=run.run_key,
                run_company_ids=tuple(row.id for row in company_rows),
            )
            uow.commit()
            return plan

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]:
        if not isinstance(run_company_id, UUID) or not isinstance(task_id, str) or not task_id.strip():
            raise QuarterlyBatchError(
                "INVALID_RUN_COMPANY_REQUEST",
                "run_company_id must be a UUID and task_id must be non-empty",
            )

        with self._uow_factory() as uow:
            candidate = uow.risks.get_run_company(run_company_id)
            if candidate is None:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_NOT_FOUND",
                    f"run company {run_company_id} was not found",
                )
            run = uow.risks.get_run(candidate.run_id, for_share=True)
            if run is None:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_FOUND",
                    f"run {candidate.run_id} was not found",
                )
            run_company = uow.risks.get_run_company(run_company_id, for_update=True)
            if run_company is None or run_company.run_id != run.id:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_IDENTITY_MISMATCH",
                    "run-company state changed while its parent run was locked",
                )
            normalized_task_id = task_id.strip()
            automatic_retry = (
                run_company.status
                in {
                    MonitoringRunCompanyStatus.FAILED,
                    MonitoringRunCompanyStatus.RETRY_PENDING,
                }
                and run_company.retryable
                and run_company.celery_task_id == normalized_task_id
            )
            if (
                run_company.status != MonitoringRunCompanyStatus.PENDING
                and not automatic_retry
            ):
                return _company_outcome(run_company)
            if run.status != MonitoringRunStatus.RUNNING:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_RUNNING",
                    "pending companies can execute only while the batch is RUNNING",
                )
            member = uow.session.scalar(
                select(SnapshotSetMember).where(
                    SnapshotSetMember.id == run_company.snapshot_set_member_id,
                    SnapshotSetMember.snapshot_set_id == run.snapshot_set_id,
                )
            )
            if member is None:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_MEMBER_MISMATCH",
                    "run-company state does not resolve to its frozen snapshot-set member",
                )

            _begin_attempt(run_company, task_id=normalized_task_id)
            uow.session.flush()
            try:
                raw_result = self._company_runner_factory().execute(
                    run_id=run.id,
                    snapshot_id=member.snapshot_id,
                )
                if not isinstance(raw_result, QuarterlyRunResult):
                    raise TypeError("company runner returned an invalid result")
            except QuarterlyRunError as error:
                _finish_blocked(run_company, error)
            except Exception as error:
                _finish_failed(run_company, error)
            else:
                _finish_succeeded(run_company, raw_result)
            outcome = _company_outcome(run_company)
            uow.commit()
            return outcome

    def prepare_automatic_retry(
        self,
        *,
        run_company_id: UUID,
        task_id: str,
    ) -> bool:
        """Reserve a retryable failed row for its owning Celery task."""

        if not isinstance(run_company_id, UUID) or not isinstance(task_id, str) or not task_id.strip():
            raise QuarterlyBatchError(
                "INVALID_RUN_COMPANY_REQUEST",
                "run_company_id must be a UUID and task_id must be non-empty",
            )
        normalized_task_id = task_id.strip()
        with self._uow_factory() as uow:
            candidate = uow.risks.get_run_company(run_company_id)
            if candidate is None:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_NOT_FOUND",
                    f"run company {run_company_id} was not found",
                )
            run = uow.risks.get_run(candidate.run_id, for_share=True)
            if run is None:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_FOUND",
                    f"run {candidate.run_id} was not found",
                )
            run_company = uow.risks.get_run_company(run_company_id, for_update=True)
            if run_company is None or run_company.run_id != run.id:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_IDENTITY_MISMATCH",
                    "run-company state changed while its parent run was locked",
                )
            owned = run_company.celery_task_id == normalized_task_id
            if (
                run_company.status == MonitoringRunCompanyStatus.RETRY_PENDING
                and run_company.retryable
                and owned
            ):
                return True
            if (
                run.status != MonitoringRunStatus.RUNNING
                or run_company.status != MonitoringRunCompanyStatus.FAILED
                or not run_company.retryable
                or not owned
            ):
                return False
            run_company.status = MonitoringRunCompanyStatus.RETRY_PENDING
            uow.commit()
            return True

    def reconcile_header_results(
        self,
        *,
        run_id: UUID,
        header_results: list[dict[str, object]],
    ) -> None:
        """Persist terminal task-boundary failures before computing the DB summary."""

        if not isinstance(run_id, UUID) or not isinstance(header_results, list):
            raise QuarterlyBatchError(
                "INVALID_HEADER_RESULTS",
                "run_id must be a UUID and header_results must be a list",
            )
        emergencies = _emergency_failures(header_results)
        if not emergencies:
            return
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_share=True)
            if run is None:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_FOUND",
                    f"run {run_id} was not found",
                )
            for run_company_id in sorted(emergencies, key=str):
                task_id = emergencies[run_company_id]
                run_company = uow.risks.get_run_company(
                    run_company_id,
                    for_update=True,
                )
                if run_company is None or run_company.run_id != run.id:
                    raise QuarterlyBatchError(
                        "RUN_COMPANY_IDENTITY_MISMATCH",
                        "emergency result does not belong to the summarized run",
                    )
                if run_company.status not in {
                    MonitoringRunCompanyStatus.PENDING,
                    MonitoringRunCompanyStatus.RUNNING,
                    MonitoringRunCompanyStatus.RETRY_PENDING,
                }:
                    continue
                if (
                    run_company.status
                    in {
                        MonitoringRunCompanyStatus.RUNNING,
                        MonitoringRunCompanyStatus.RETRY_PENDING,
                    }
                    and run_company.celery_task_id != task_id
                ):
                    # A duplicate canvas cannot exhaust or steal another task's
                    # in-flight attempt or scheduled retry ownership.
                    continue
                if run_company.status in {
                    MonitoringRunCompanyStatus.PENDING,
                    MonitoringRunCompanyStatus.RETRY_PENDING,
                }:
                    _begin_attempt(run_company, task_id=task_id)
                else:
                    run_company.celery_task_id = task_id
                _finish_failed_values(
                    run_company,
                    error_code=EMERGENCY_FAILURE_CODE,
                    error_message=(
                        "Celery task failed before its company result could be persisted"
                    ),
                )
            uow.commit()

    def summarize(self, *, run_id: UUID) -> dict[str, object]:
        if not isinstance(run_id, UUID):
            raise QuarterlyBatchError("INVALID_RUN_ID", "run_id must be a UUID")
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_update=True)
            if run is None:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_FOUND",
                    f"run {run_id} was not found",
                )
            counts = uow.risks.run_company_status_counts(run.id)
            total = sum(counts.values())
            if total != run.requested_company_count:
                raise QuarterlyBatchError(
                    "RUN_COMPANY_COUNT_MISMATCH",
                    "durable company-state count does not match the requested batch size",
                )
            succeeded = counts.get(MonitoringRunCompanyStatus.SUCCEEDED, 0)
            blocked = counts.get(MonitoringRunCompanyStatus.BLOCKED, 0)
            failed = counts.get(MonitoringRunCompanyStatus.FAILED, 0)
            active = counts.get(MonitoringRunCompanyStatus.PENDING, 0) + counts.get(
                MonitoringRunCompanyStatus.RUNNING,
                0,
            )
            active += counts.get(MonitoringRunCompanyStatus.RETRY_PENDING, 0)
            if active:
                status = MonitoringRunStatus.RUNNING
                finished_at = None
            elif succeeded == run.requested_company_count:
                status = MonitoringRunStatus.SUCCEEDED
                finished_at = run.finished_at or _utcnow()
            elif succeeded:
                status = MonitoringRunStatus.PARTIAL_SUCCESS
                finished_at = run.finished_at or _utcnow()
            else:
                status = MonitoringRunStatus.FAILED
                finished_at = run.finished_at or _utcnow()

            run.status = status
            run.succeeded_company_count = succeeded
            run.blocked_company_count = blocked
            run.failed_company_count = failed
            run.finished_at = finished_at
            run.failure_reason = (
                "NO_COMPANY_SUCCEEDED" if status == MonitoringRunStatus.FAILED else None
            )
            summary = _batch_summary(run)
            uow.commit()
            return summary

    def retry_failed(self, *, run_id: UUID) -> QuarterlyBatchPlan:
        if not isinstance(run_id, UUID):
            raise QuarterlyBatchError("INVALID_RUN_ID", "run_id must be a UUID")
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_update=True)
            if run is None:
                raise QuarterlyBatchError(
                    "MONITORING_RUN_NOT_FOUND",
                    f"run {run_id} was not found",
                )
            if run.status not in {
                MonitoringRunStatus.PARTIAL_SUCCESS,
                MonitoringRunStatus.FAILED,
            }:
                raise QuarterlyBatchError(
                    "QUARTERLY_BATCH_RETRY_NOT_ALLOWED",
                    "failed companies can be retried only from a terminal failed batch",
                )
            failed_rows = uow.risks.list_run_companies(
                run.id,
                statuses=(MonitoringRunCompanyStatus.FAILED,),
                for_update=True,
            )
            if not failed_rows:
                return QuarterlyBatchPlan(run.id, run.run_key, ())
            for row in failed_rows:
                _reset_for_retry(row)
            uow.session.flush()
            counts = uow.risks.run_company_status_counts(run.id)
            run.status = MonitoringRunStatus.RUNNING
            run.succeeded_company_count = counts.get(
                MonitoringRunCompanyStatus.SUCCEEDED,
                0,
            )
            run.blocked_company_count = counts.get(MonitoringRunCompanyStatus.BLOCKED, 0)
            run.failed_company_count = 0
            run.finished_at = None
            run.failure_reason = None
            plan = QuarterlyBatchPlan(
                run_id=run.id,
                run_key=run.run_key,
                run_company_ids=tuple(row.id for row in failed_rows),
            )
            uow.commit()
            return plan


def _validate_batch_request(
    *,
    fiscal_year: int,
    quarter: int,
    snapshot_set_id: UUID,
    rule_version_id: UUID,
) -> None:
    if (
        not isinstance(fiscal_year, int)
        or isinstance(fiscal_year, bool)
        or not 2000 <= fiscal_year <= 9999
        or not isinstance(quarter, int)
        or isinstance(quarter, bool)
        or quarter not in {1, 2, 3, 4}
        or not isinstance(snapshot_set_id, UUID)
        or not isinstance(rule_version_id, UUID)
    ):
        raise QuarterlyBatchError(
            "INVALID_QUARTERLY_BATCH_REQUEST",
            "year, quarter, snapshot_set_id, and rule_version_id are invalid",
        )


def _run_key(
    *,
    fiscal_year: int,
    quarter: int,
    snapshot_set_id: UUID,
    rule_version_id: UUID,
) -> str:
    return f"quarterly:{fiscal_year}:Q{quarter}:{snapshot_set_id}:{rule_version_id}"


def _lock_batch_key(uow: UnitOfWork, run_key: str) -> None:
    uow.session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:scope))"),
        {"namespace": 20260715, "scope": f"quarterly-batch:{run_key}"},
    )


def _assert_snapshot_set_ready(
    snapshot_set: SnapshotSet,
    *,
    fiscal_year: int,
    quarter: int,
) -> None:
    actual_quarter = (snapshot_set.period.month - 1) // 3 + 1
    if snapshot_set.status != SnapshotSetStatus.PUBLISHED:
        raise QuarterlyBatchError(
            "SNAPSHOT_SET_NOT_PUBLISHED",
            "quarterly batches require a published snapshot set",
        )
    if snapshot_set.period.year != fiscal_year or actual_quarter != quarter:
        raise QuarterlyBatchError(
            "SNAPSHOT_SET_PERIOD_MISMATCH",
            "snapshot-set period does not match the requested year and quarter",
        )


def _assert_rule_ready(rule: RuleVersion | None, snapshot_set: SnapshotSet) -> None:
    if (
        rule is None
        or rule.rule_code != "QUARTERLY_V1"
        or rule.version != "phase-1-reviewed"
        or rule.status != VersionStatus.PUBLISHED
        or rule.effective_from > snapshot_set.period
        or (rule.effective_to is not None and rule.effective_to < snapshot_set.period)
    ):
        raise QuarterlyBatchError(
            "QUARTERLY_RULE_NOT_EFFECTIVE",
            "batch must pin the fixed effective reviewed QUARTERLY_V1 rule",
        )
    try:
        assert_approved_quarterly_rule_manifest(rule)
    except QuarterlyRunError as error:
        raise QuarterlyBatchError(error.error_code, str(error)) from error


def _begin_attempt(run_company: MonitoringRunCompany, *, task_id: str) -> None:
    run_company.status = MonitoringRunCompanyStatus.RUNNING
    run_company.attempt_count += 1
    run_company.retryable = False
    run_company.celery_task_id = task_id.strip()
    run_company.started_at = _utcnow()
    run_company.finished_at = None
    run_company.error_code = None
    run_company.error_message = None
    run_company.detection_ids = []
    run_company.case_ids = []


def _finish_succeeded(
    run_company: MonitoringRunCompany,
    result: QuarterlyRunResult,
) -> None:
    run_company.status = MonitoringRunCompanyStatus.SUCCEEDED
    run_company.retryable = False
    run_company.finished_at = _utcnow()
    run_company.error_code = None
    run_company.error_message = None
    run_company.detection_ids = [str(value) for value in result.detection_ids]
    run_company.case_ids = [str(value) for value in result.case_ids]


def _finish_blocked(
    run_company: MonitoringRunCompany,
    error: QuarterlyRunError,
) -> None:
    run_company.status = MonitoringRunCompanyStatus.BLOCKED
    run_company.retryable = False
    run_company.finished_at = _utcnow()
    run_company.error_code = error.error_code
    run_company.error_message = _error_message(error)
    run_company.detection_ids = []
    run_company.case_ids = []


def _finish_failed(run_company: MonitoringRunCompany, error: Exception) -> None:
    _finish_failed_values(
        run_company,
        error_code="UNEXPECTED_COMPANY_FAILURE",
        error_message=_error_message(error),
    )


def _finish_failed_values(
    run_company: MonitoringRunCompany,
    *,
    error_code: str,
    error_message: str,
) -> None:
    run_company.status = MonitoringRunCompanyStatus.FAILED
    run_company.retryable = True
    run_company.finished_at = _utcnow()
    run_company.error_code = error_code
    run_company.error_message = error_message
    run_company.detection_ids = []
    run_company.case_ids = []


def _reset_for_retry(run_company: MonitoringRunCompany) -> None:
    run_company.status = MonitoringRunCompanyStatus.PENDING
    run_company.retryable = False
    run_company.celery_task_id = None
    run_company.started_at = None
    run_company.finished_at = None
    run_company.error_code = None
    run_company.error_message = None
    run_company.detection_ids = []
    run_company.case_ids = []


def _company_outcome(run_company: MonitoringRunCompany) -> dict[str, object]:
    return {
        "run_company_id": str(run_company.id),
        "status": run_company.status.value,
        "retryable": run_company.retryable,
        "task_id": run_company.celery_task_id,
        "detection_ids": list(run_company.detection_ids),
        "case_ids": list(run_company.case_ids),
        "error_code": run_company.error_code,
    }


def _batch_summary(run: MonitoringRun) -> dict[str, object]:
    return {
        "run_id": str(run.id),
        "status": run.status.value,
        "requested_company_count": run.requested_company_count,
        "succeeded_company_count": run.succeeded_company_count,
        "blocked_company_count": run.blocked_company_count,
        "failed_company_count": run.failed_company_count,
    }


def _error_message(error: Exception) -> str:
    return str(error).strip() or type(error).__name__


def _emergency_failures(
    header_results: list[dict[str, object]],
) -> dict[UUID, str]:
    emergencies: dict[UUID, str] = {}
    for result in header_results:
        if not isinstance(result, Mapping):
            raise QuarterlyBatchError(
                "INVALID_HEADER_RESULTS",
                "every quarterly header result must be an object",
            )
        if result.get("error_code") != EMERGENCY_FAILURE_CODE:
            continue
        raw_run_company_id = result.get("run_company_id")
        raw_task_id = result.get("task_id")
        if (
            result.get("status") != MonitoringRunCompanyStatus.FAILED.value
            or not isinstance(raw_run_company_id, str)
            or not isinstance(raw_task_id, str)
            or not raw_task_id.strip()
            or len(raw_task_id.strip()) > 255
        ):
            raise QuarterlyBatchError(
                "INVALID_HEADER_RESULTS",
                "emergency quarterly header result has an invalid identity",
            )
        try:
            run_company_id = UUID(raw_run_company_id)
        except ValueError as error:
            raise QuarterlyBatchError(
                "INVALID_HEADER_RESULTS",
                "emergency quarterly header result has an invalid run-company id",
            ) from error
        emergencies[run_company_id] = raw_task_id.strip()
    return emergencies


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "EMERGENCY_FAILURE_CODE",
    "QuarterlyBatchError",
    "QuarterlyBatchPlan",
    "QuarterlyBatchService",
]

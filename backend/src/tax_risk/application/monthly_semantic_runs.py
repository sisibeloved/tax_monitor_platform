"""Frozen monthly semantic run control plane for welfare and donation monitoring."""

from __future__ import annotations

import asyncio
from calendar import monthrange
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text

from tax_risk.application.semantic.sap_voucher_monitor import (
    MonitorRunResult,
    SapVoucherMonitor,
)
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import SemanticVersionSet
from tax_risk.observability.delivery import derive_batch_delivery
from tax_risk.observability.metrics import record_company_task
from tax_risk.persistence.ingest_models import Company, CompanyLifecycle
from tax_risk.persistence.master_models import RuleVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    MonitoringRun,
    MonitoringRunCompany,
    MonitoringRunCompanyStatus,
    MonitoringRunStatus,
    MonitoringRunType,
)
from tax_risk.persistence.semantic_models import (
    SemanticArtifactVersion,
    SemanticVersionSetRecord,
    SuggestedAccountDictionaryVersion,
)
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
    SnapshotStatus,
)


UowFactory = Callable[[], UnitOfWork]


class MonitorFactory(Protocol):
    def __call__(
        self,
        *,
        uow: UnitOfWork,
        monitoring_type: MonitorType,
        versions: SemanticVersionSet,
    ) -> SapVoucherMonitor: ...


class MonthlySemanticRunError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FrozenSemanticVersions:
    rule_version: str
    model_version: str
    prompt_version: str
    case_library_version: str
    account_dictionary_version: str


@dataclass(frozen=True, slots=True)
class MonthlyRunCompanyView:
    id: UUID
    company_id: UUID
    company_code: str
    snapshot_id: UUID
    status: str
    selected: bool | None
    adjustment_amount: Decimal | None
    processed_line_count: int
    risk_case_count: int
    issue_code: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class MonthlyRunView:
    run_id: UUID
    run_key: str
    monitoring_type: MonitorType
    period: str
    status: str
    snapshot_set_id: UUID
    semantic_version_set_id: UUID
    frozen_versions: FrozenSemanticVersions
    requested_company_count: int
    succeeded_company_count: int
    failed_company_count: int
    not_run_company_count: int
    failure_reason: str | None
    companies: tuple[MonthlyRunCompanyView, ...]


@dataclass(frozen=True, slots=True)
class MonthlyRunPlan:
    run: MonthlyRunView
    run_company_ids: tuple[UUID, ...]


class MonthlySemanticRunService:
    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        monitor_factory: MonitorFactory | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._monitor_factory = monitor_factory

    def start_run(
        self,
        *,
        monitoring_type: MonitorType,
        period: str,
        company_codes: Sequence[str],
        snapshot_set_id: UUID,
        semantic_version_set_id: UUID,
        allowed_company_ids: frozenset[UUID] | None,
    ) -> MonthlyRunPlan:
        if monitoring_type not in {MonitorType.WELFARE, MonitorType.DONATION}:
            raise MonthlySemanticRunError(
                "MONTHLY_MONITOR_TYPE_INVALID", "only WELFARE and DONATION are supported"
            )
        period_end = _month_end(period)
        normalized_codes = tuple(code.strip() for code in company_codes)
        if not normalized_codes or any(not code for code in normalized_codes):
            raise MonthlySemanticRunError(
                "MONTHLY_COMPANY_REQUIRED", "at least one company code is required"
            )
        if len(set(normalized_codes)) != len(normalized_codes):
            raise MonthlySemanticRunError(
                "MONTHLY_COMPANY_DUPLICATE", "company codes must be unique"
            )
        run_key = (
            f"MONTHLY_SEMANTIC:{period}:{snapshot_set_id}:"
            f"{semantic_version_set_id}:{monitoring_type.value}"
        )
        with self._uow_factory() as uow:
            uow.session.execute(
                text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:key))"),
                {"namespace": 20260716, "key": run_key},
            )
            existing = uow.risks.get_run_by_key(run_key)
            if existing is not None:
                view = _run_view(uow, existing, allowed_company_ids)
                pending = tuple(
                    item.id
                    for item in uow.risks.list_run_companies(
                        existing.id,
                        statuses=(MonitoringRunCompanyStatus.PENDING,),
                    )
                )
                if (
                    existing.status == MonitoringRunStatus.FAILED
                    and existing.failure_reason == "BROKER_DISPATCH_FAILED"
                    and pending
                ):
                    existing.status = MonitoringRunStatus.RUNNING
                    existing.failed_company_count = 0
                    existing.finished_at = None
                    existing.batch_finished_at = None
                    existing.output_ready_at = None
                    existing.failure_reason = None
                    uow.session.flush()
                    view = _run_view(uow, existing, allowed_company_ids)
                    uow.commit()
                return MonthlyRunPlan(view, pending)

            snapshot_set = uow.session.scalar(
                select(SnapshotSet)
                .where(SnapshotSet.id == snapshot_set_id)
                .with_for_update(read=True)
            )
            if snapshot_set is None:
                raise MonthlySemanticRunError(
                    "SNAPSHOT_SET_NOT_FOUND", "snapshot set was not found"
                )
            if (
                snapshot_set.status != SnapshotSetStatus.PUBLISHED
                or snapshot_set.published_at is None
            ):
                raise MonthlySemanticRunError(
                    "SNAPSHOT_SET_NOT_PUBLISHED", "snapshot set must be published"
                )
            if snapshot_set.period != period_end:
                raise MonthlySemanticRunError(
                    "SNAPSHOT_SET_PERIOD_MISMATCH", "snapshot set period does not match"
                )
            version_record, _ = _load_frozen_versions(
                uow,
                semantic_version_set_id,
                period_end,
            )
            member_rows = uow.session.execute(
                select(SnapshotSetMember, Company, AccountingSnapshot)
                .join(Company, Company.id == SnapshotSetMember.company_id)
                .join(AccountingSnapshot, AccountingSnapshot.id == SnapshotSetMember.snapshot_id)
                .where(
                    SnapshotSetMember.snapshot_set_id == snapshot_set.id,
                    Company.company_code.in_(normalized_codes),
                )
                .order_by(Company.company_code)
                .with_for_update(read=True)
            ).all()
            if len(member_rows) != len(normalized_codes):
                raise MonthlySemanticRunError(
                    "MONTHLY_COMPANY_NOT_MEMBER",
                    "every requested company must be a snapshot-set member",
                )
            if any(
                company.lifecycle != CompanyLifecycle.ACTIVE
                or snapshot.status != SnapshotStatus.PUBLISHED
                or snapshot.period != period_end
                for _, company, snapshot in member_rows
            ):
                raise MonthlySemanticRunError(
                    "MONTHLY_COMPANY_NOT_READY",
                    "requested company snapshots must remain active and published",
                )
            if allowed_company_ids is not None and any(
                company.id not in allowed_company_ids for _, company, _ in member_rows
            ):
                raise MonthlySemanticRunError(
                    "MONTHLY_COMPANY_OUT_OF_SCOPE", "requested company is outside principal scope"
                )

            now = _utcnow()
            run = MonitoringRun(
                run_key=run_key,
                run_type=MonitoringRunType.MONTHLY_SEMANTIC,
                snapshot_set_id=snapshot_set.id,
                rule_version_id=version_record.rule_version_id,
                status=MonitoringRunStatus.RUNNING,
                fiscal_year=period_end.year,
                quarter=None,
                period=period_end,
                monitoring_type=monitoring_type,
                semantic_version_set_id=version_record.id,
                requested_company_count=len(member_rows),
                succeeded_company_count=0,
                failed_company_count=0,
                blocked_company_count=0,
                started_at=now,
                finished_at=None,
                failure_reason=None,
            )
            uow.risks.add_run(run)
            uow.session.flush()
            company_models = [
                MonitoringRunCompany(
                    run_id=run.id,
                    snapshot_set_id=run.snapshot_set_id,
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
                    selected=None,
                    adjustment_amount=None,
                    processed_line_count=0,
                    risk_case_count=0,
                    issue_code=None,
                )
                for member, _, _ in member_rows
            ]
            uow.session.add_all(company_models)
            uow.session.flush()
            view = _run_view(uow, run, allowed_company_ids)
            plan = MonthlyRunPlan(view, tuple(item.id for item in company_models))
            uow.commit()
            return plan

    def get_status(
        self,
        run_id: UUID,
        *,
        allowed_company_ids: frozenset[UUID] | None,
    ) -> MonthlyRunView:
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id)
            if run is None or run.run_type != MonitoringRunType.MONTHLY_SEMANTIC:
                raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
            return _run_view(uow, run, allowed_company_ids)

    def mark_dispatch_failed(self, run_id: UUID) -> None:
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_update=True)
            if run is None:
                raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
            run.status = MonitoringRunStatus.FAILED
            run.failed_company_count = run.requested_company_count
            run.finished_at = _utcnow()
            run.batch_finished_at = run.finished_at
            run.output_ready_at = None
            run.failure_reason = "BROKER_DISPATCH_FAILED"
            uow.commit()

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]:
        if not task_id.strip():
            raise MonthlySemanticRunError("MONTHLY_TASK_ID_INVALID", "task id is required")
        with self._uow_factory() as uow:
            row = uow.risks.get_run_company(run_company_id, for_update=True)
            if row is None:
                raise MonthlySemanticRunError("MONTHLY_RUN_COMPANY_NOT_FOUND", "row not found")
            run = uow.risks.get_run(row.run_id, for_share=True)
            if run is None or run.run_type != MonitoringRunType.MONTHLY_SEMANTIC:
                raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
            if row.status not in {
                MonitoringRunCompanyStatus.PENDING,
                MonitoringRunCompanyStatus.FAILED,
            }:
                return _company_outcome(row)
            member, company = uow.session.execute(
                select(SnapshotSetMember, Company)
                .join(Company, Company.id == SnapshotSetMember.company_id)
                .where(
                    SnapshotSetMember.id == row.snapshot_set_member_id,
                    SnapshotSetMember.snapshot_set_id == run.snapshot_set_id,
                )
            ).one()
            assert run.period is not None and run.semantic_version_set_id is not None
            assert run.monitoring_type is not None
            _, versions = _load_frozen_versions(
                uow,
                run.semantic_version_set_id,
                run.period,
            )
            row.status = MonitoringRunCompanyStatus.RUNNING
            row.attempt_count += 1
            row.retryable = False
            row.celery_task_id = task_id.strip()
            row.started_at = _utcnow()
            row.finished_at = None
            row.company_output_ready_at = None
            row.error_code = None
            row.error_message = None
            row.issue_code = None
            uow.session.flush()
            try:
                if self._monitor_factory is None:
                    raise RuntimeError("monthly monitor factory is not configured")
                monitor = self._monitor_factory(
                    uow=uow,
                    monitoring_type=run.monitoring_type,
                    versions=versions,
                )
                result = asyncio.run(
                    monitor.run(
                        company.company_code,
                        run.period.strftime("%Y-%m"),
                        run.snapshot_set_id,
                        member.snapshot_id,
                    )
                )
                _finish_company(row, result)
            except Exception as error:
                row.status = MonitoringRunCompanyStatus.FAILED
                row.retryable = True
                row.finished_at = _utcnow()
                row.company_output_ready_at = None
                row.error_code = "MONTHLY_COMPANY_EXECUTION_FAILED"
                row.error_message = str(error)[:2000]
                row.detection_ids = []
                row.case_ids = []
                row.selected = None
                row.adjustment_amount = None
                row.processed_line_count = 0
                row.risk_case_count = 0
                row.issue_code = None
            outcome = _company_outcome(row)
            record_company_task(
                run_type="MONTHLY_SEMANTIC",
                monitor_type=run.monitoring_type.value,
                status=row.status.value,
                error_code=row.error_code or row.issue_code,
            )
            uow.commit()
            return outcome

    def summarize(self, run_id: UUID) -> dict[str, object]:
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_update=True)
            if run is None:
                raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
            counts = uow.risks.run_company_status_counts(run.id)
            succeeded = counts.get(MonitoringRunCompanyStatus.SUCCEEDED, 0)
            failed = counts.get(MonitoringRunCompanyStatus.FAILED, 0)
            not_run = counts.get(MonitoringRunCompanyStatus.NOT_RUN, 0)
            active = sum(
                counts.get(status, 0)
                for status in (
                    MonitoringRunCompanyStatus.PENDING,
                    MonitoringRunCompanyStatus.RUNNING,
                    MonitoringRunCompanyStatus.RETRY_PENDING,
                )
            )
            if active:
                status = MonitoringRunStatus.RUNNING
            elif failed == 0 and not_run == 0:
                status = MonitoringRunStatus.SUCCEEDED
            elif succeeded:
                status = MonitoringRunStatus.PARTIAL_SUCCESS
            else:
                status = MonitoringRunStatus.FAILED
            run.status = status
            run.succeeded_company_count = succeeded
            run.failed_company_count = failed
            run.blocked_company_count = not_run
            run.finished_at = None if active else _utcnow()
            company_rows = uow.risks.list_run_companies(run.id)
            delivery = derive_batch_delivery(
                (
                    (row.status.value, row.company_output_ready_at)
                    for row in company_rows
                ),
                now=run.batch_finished_at or _utcnow(),
            )
            run.batch_finished_at = delivery.batch_finished_at
            run.output_ready_at = delivery.output_ready_at
            run.failure_reason = "NO_COMPANY_SUCCEEDED" if status is MonitoringRunStatus.FAILED else None
            result: dict[str, object] = {
                "run_id": str(run.id),
                "status": status.value,
                "succeeded": succeeded,
                "failed": failed,
                "not_run": not_run,
            }
            uow.commit()
            return result

    def retry_failed(self, run_id: UUID) -> tuple[UUID, ...]:
        with self._uow_factory() as uow:
            run = uow.risks.get_run(run_id, for_update=True)
            if run is None:
                raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
            rows = uow.risks.list_run_companies(
                run.id,
                statuses=(MonitoringRunCompanyStatus.FAILED,),
                for_update=True,
            )
            for row in rows:
                row.status = MonitoringRunCompanyStatus.PENDING
                row.retryable = False
                row.celery_task_id = None
                row.started_at = None
                row.finished_at = None
                row.company_output_ready_at = None
                row.error_code = None
                row.error_message = None
                row.detection_ids = []
                row.case_ids = []
            run.status = MonitoringRunStatus.RUNNING
            run.finished_at = None
            run.batch_finished_at = None
            run.output_ready_at = None
            run.failure_reason = None
            ids = tuple(row.id for row in rows)
            uow.commit()
            return ids


def _load_frozen_versions(
    uow: UnitOfWork,
    version_set_id: UUID,
    effective_on: date,
) -> tuple[SemanticVersionSetRecord, SemanticVersionSet]:
    record = uow.semantic.get_semantic_version_set(version_set_id, for_update=True)
    if (
        record is None
        or record.status != "PUBLISHED"
        or not record.effective_from <= effective_on <= record.effective_to
    ):
        raise MonthlySemanticRunError(
            "SEMANTIC_VERSION_SET_NOT_PUBLISHED",
            "semantic version set must be published and effective",
        )
    rule = uow.session.get(RuleVersion, record.rule_version_id)
    artifacts = {
        artifact.id: artifact
        for artifact in uow.session.scalars(
            select(SemanticArtifactVersion).where(
                SemanticArtifactVersion.id.in_(
                    (
                        record.model_artifact_id,
                        record.prompt_artifact_id,
                        record.case_library_artifact_id,
                    )
                )
            )
        )
    }
    dictionary = uow.session.get(
        SuggestedAccountDictionaryVersion,
        record.account_dictionary_version_id,
    )
    if (
        rule is None
        or rule.status != VersionStatus.PUBLISHED
        or rule.effective_from > effective_on
        or (rule.effective_to is not None and rule.effective_to < effective_on)
        or dictionary is None
        or dictionary.status != "PUBLISHED"
        or not dictionary.effective_from <= effective_on <= dictionary.effective_to
    ):
        raise MonthlySemanticRunError(
            "SEMANTIC_VERSION_MEMBER_NOT_PUBLISHED",
            "every frozen semantic version must be published and effective",
        )
    typed = (
        (record.model_artifact_id, "MODEL"),
        (record.prompt_artifact_id, "PROMPT"),
        (record.case_library_artifact_id, "CASE_LIBRARY"),
    )
    if any(
        artifact_id not in artifacts
        or artifacts[artifact_id].artifact_type != expected_type
        or artifacts[artifact_id].status != "PUBLISHED"
        or not artifacts[artifact_id].effective_from <= effective_on
        <= artifacts[artifact_id].effective_to
        for artifact_id, expected_type in typed
    ):
        raise MonthlySemanticRunError(
            "SEMANTIC_VERSION_MEMBER_NOT_PUBLISHED",
            "every frozen semantic version must be published and effective",
        )
    return record, SemanticVersionSet(
        rule_version_id=rule.version,
        model_version_id=artifacts[record.model_artifact_id].version,
        prompt_version_id=artifacts[record.prompt_artifact_id].version,
        case_library_version_id=artifacts[record.case_library_artifact_id].version,
        account_dictionary_version=dictionary.dictionary_version,
    )


def _run_view(
    uow: UnitOfWork,
    run: MonitoringRun,
    allowed_company_ids: frozenset[UUID] | None,
) -> MonthlyRunView:
    assert run.period is not None and run.monitoring_type is not None
    assert run.semantic_version_set_id is not None
    _, versions = _load_frozen_versions(uow, run.semantic_version_set_id, run.period)
    statement = (
        select(MonitoringRunCompany, SnapshotSetMember, Company)
        .join(SnapshotSetMember, SnapshotSetMember.id == MonitoringRunCompany.snapshot_set_member_id)
        .join(Company, Company.id == SnapshotSetMember.company_id)
        .where(MonitoringRunCompany.run_id == run.id)
        .order_by(Company.company_code)
    )
    if allowed_company_ids is not None:
        if not allowed_company_ids:
            raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
        statement = statement.where(Company.id.in_(allowed_company_ids))
    rows = uow.session.execute(statement).all()
    if not rows:
        raise MonthlySemanticRunError("MONTHLY_RUN_NOT_FOUND", "run was not found")
    companies = tuple(
        MonthlyRunCompanyView(
            id=row.id,
            company_id=company.id,
            company_code=company.company_code,
            snapshot_id=member.snapshot_id,
            status=row.status.value,
            selected=row.selected,
            adjustment_amount=row.adjustment_amount,
            processed_line_count=row.processed_line_count,
            risk_case_count=row.risk_case_count,
            issue_code=row.issue_code,
            attempt_count=row.attempt_count,
        )
        for row, member, company in rows
    )
    return MonthlyRunView(
        run_id=run.id,
        run_key=run.run_key,
        monitoring_type=run.monitoring_type,
        period=run.period.strftime("%Y-%m"),
        status=run.status.value,
        snapshot_set_id=run.snapshot_set_id,
        semantic_version_set_id=run.semantic_version_set_id,
        frozen_versions=FrozenSemanticVersions(
            rule_version=versions.rule_version_id,
            model_version=versions.model_version_id,
            prompt_version=versions.prompt_version_id,
            case_library_version=versions.case_library_version_id,
            account_dictionary_version=versions.account_dictionary_version,
        ),
        requested_company_count=len(companies),
        succeeded_company_count=sum(item.status == "SUCCEEDED" for item in companies),
        failed_company_count=sum(item.status == "FAILED" for item in companies),
        not_run_company_count=sum(item.status == "NOT_RUN" for item in companies),
        failure_reason=run.failure_reason,
        companies=companies,
    )


def _finish_company(row: MonitoringRunCompany, result: MonitorRunResult) -> None:
    row.status = (
        MonitoringRunCompanyStatus.NOT_RUN
        if result.status == "NOT_RUN"
        else MonitoringRunCompanyStatus.SUCCEEDED
    )
    row.retryable = False
    row.finished_at = _utcnow()
    row.company_output_ready_at = (
        row.finished_at if row.status == MonitoringRunCompanyStatus.SUCCEEDED else None
    )
    row.selected = result.selected
    row.adjustment_amount = (
        Decimal(result.adjustment) if result.adjustment is not None else None
    )
    row.processed_line_count = result.processed_lines
    row.risk_case_count = result.created_or_updated_cases
    row.issue_code = result.issue_code
    row.error_code = None
    row.error_message = None
    row.detection_ids = [str(value) for value in result.detection_ids]
    row.case_ids = [str(value) for value in result.case_ids]


def _company_outcome(row: MonitoringRunCompany) -> dict[str, object]:
    return {
        "run_company_id": str(row.id),
        "status": row.status.value,
        "retryable": row.retryable,
        "task_id": row.celery_task_id,
        "detection_ids": list(row.detection_ids),
        "case_ids": list(row.case_ids),
        "issue_code": row.issue_code,
        "error_code": row.error_code,
    }


def _month_end(period: str) -> date:
    try:
        year, month = (int(value) for value in period.split("-", maxsplit=1))
        return date(year, month, monthrange(year, month)[1])
    except (TypeError, ValueError) as error:
        raise MonthlySemanticRunError(
            "MONTHLY_PERIOD_INVALID", "period must use YYYY-MM format"
        ) from error


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "FrozenSemanticVersions",
    "MonthlyRunCompanyView",
    "MonthlyRunPlan",
    "MonthlyRunView",
    "MonthlySemanticRunError",
    "MonthlySemanticRunService",
]

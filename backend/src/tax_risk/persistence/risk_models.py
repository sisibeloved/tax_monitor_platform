from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.domain.cases import MonitorType
from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class MonitoringRunType(StrEnum):
    QUARTERLY = "QUARTERLY"
    MONTHLY_SEMANTIC = "MONTHLY_SEMANTIC"


class MonitoringRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class MonitoringRunCompanyStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_PENDING = "RETRY_PENDING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


class CalculationStatus(StrEnum):
    CALCULATED = "CALCULATED"
    NOT_CALCULABLE = "NOT_CALCULABLE"
    FAILED = "FAILED"


class RiskCaseStatus(StrEnum):
    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    PENDING_COMPANY_CONFIRMATION = "PENDING_COMPANY_CONFIRMATION"
    PENDING_ADJUSTMENT = "PENDING_ADJUSTMENT"
    ADJUSTED_PENDING_REVIEW = "ADJUSTED_PENDING_REVIEW"
    CLOSED = "CLOSED"
    GROUP_REVIEW = "GROUP_REVIEW"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"


class MonitoringRun(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "monitoring_run"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_monitoring_run_key"),
        UniqueConstraint(
            "id",
            "snapshot_set_id",
            name="uq_monitoring_run_id_snapshot_set",
        ),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 9999", name="fiscal_year"),
        CheckConstraint("quarter BETWEEN 1 AND 4", name="quarter"),
        CheckConstraint(
            "(run_type = 'QUARTERLY' AND quarter IS NOT NULL "
            "AND period IS NULL AND monitoring_type IS NULL "
            "AND semantic_version_set_id IS NULL) OR "
            "(run_type = 'MONTHLY_SEMANTIC' AND quarter IS NULL "
            "AND period IS NOT NULL AND monitoring_type IS NOT NULL "
            "AND semantic_version_set_id IS NOT NULL)",
            name="run_contract",
        ),
        CheckConstraint(
            "requested_company_count >= 0 AND succeeded_company_count >= 0 AND "
            "failed_company_count >= 0 AND blocked_company_count >= 0",
            name="nonnegative_counts",
        ),
    )

    run_key: Mapped[str] = mapped_column(String(512), nullable=False)
    run_type: Mapped[MonitoringRunType] = mapped_column(
        Enum(MonitoringRunType, name="monitoring_run_type"), nullable=False
    )
    snapshot_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("snapshot_set.id", ondelete="RESTRICT"), nullable=False
    )
    rule_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_version.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[MonitoringRunStatus] = mapped_column(
        Enum(MonitoringRunStatus, name="monitoring_run_status"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int | None] = mapped_column(SmallInteger)
    period: Mapped[date | None] = mapped_column(Date)
    monitoring_type: Mapped[MonitorType | None] = mapped_column(
        Enum(MonitorType, name="monitor_type", create_type=False)
    )
    semantic_version_set_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_version_set.id", ondelete="RESTRICT")
    )
    requested_company_count: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded_company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failed_company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    blocked_company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class MonitoringRunCompany(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "monitoring_run_company"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "snapshot_set_id"],
            ["monitoring_run.id", "monitoring_run.snapshot_set_id"],
            name="fk_run_company_run_snapshot_set",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_set_member_id", "snapshot_set_id"],
            ["snapshot_set_member.id", "snapshot_set_member.snapshot_set_id"],
            name="fk_run_company_member_snapshot_set",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "snapshot_set_member_id",
            name="uq_monitoring_run_company_run_member",
        ),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint(
            "processed_line_count >= 0 AND risk_case_count >= 0",
            name="nonnegative_monthly_counts",
        ),
        CheckConstraint(
            "jsonb_typeof(detection_ids) = 'array' AND jsonb_typeof(case_ids) = 'array'",
            name="result_ids_are_arrays",
        ),
        CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name="attempt_time_order",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND retryable = false "
            "AND celery_task_id IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND error_code IS NULL AND error_message IS NULL "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'RUNNING' AND attempt_count > 0 AND retryable = false "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_code IS NULL AND error_message IS NULL "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'RETRY_PENDING' AND attempt_count > 0 AND retryable = true "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
            "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'SUCCEEDED' AND attempt_count > 0 AND retryable = false "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL) OR "
            "(status = 'BLOCKED' AND attempt_count > 0 AND retryable = false "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
            "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'FAILED' AND attempt_count > 0 AND retryable = true "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND btrim(error_code) <> '' "
            "AND error_message IS NOT NULL AND btrim(error_message) <> '' "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb) OR "
            "(status = 'NOT_RUN' AND attempt_count > 0 AND retryable = false "
            "AND celery_task_id IS NOT NULL AND btrim(celery_task_id) <> '' "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND issue_code IS NOT NULL AND btrim(issue_code) <> '' "
            "AND detection_ids = '[]'::jsonb AND case_ids = '[]'::jsonb)",
            name="lifecycle_state",
        ),
        Index("ix_monitoring_run_company_run_status", "run_id", "status"),
        Index(
            "ix_monitoring_run_company_snapshot_set_member_id",
            "snapshot_set_member_id",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot_set_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot_set_member_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[MonitoringRunCompanyStatus] = mapped_column(
        Enum(MonitoringRunCompanyStatus, name="monitoring_run_company_status"),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    retryable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    detection_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    case_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    selected: Mapped[bool | None] = mapped_column(Boolean)
    adjustment_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    processed_line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    risk_case_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    issue_code: Mapped[str | None] = mapped_column(String(128))


class DetectionRecord(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "detection_record"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "company_id", "tax_master_version_id"],
            [
                "accounting_snapshot.id",
                "accounting_snapshot.company_id",
                "accounting_snapshot.tax_master_version_id",
            ],
            name="fk_detection_snapshot_company_master",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("detection_key", name="uq_detection_record_key"),
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("rate_value IS NULL OR rate_value BETWEEN 0 AND 1", name="rate_value"),
        CheckConstraint(
            "(calculation_status = 'CALCULATED' AND not_calculated_reason IS NULL AND "
            "((monitor_type = 'TAX_BURDEN' AND result_amount IS NULL AND "
            "tax_burden_rate IS NOT NULL AND tax_burden_deviation IS NOT NULL) OR "
            "(monitor_type <> 'TAX_BURDEN' AND result_amount IS NOT NULL AND "
            "tax_burden_rate IS NULL AND tax_burden_deviation IS NULL))) OR "
            "(calculation_status IN ('NOT_CALCULABLE', 'FAILED') AND "
            "result_amount IS NULL AND tax_burden_rate IS NULL AND "
            "tax_burden_deviation IS NULL AND not_calculated_reason IS NOT NULL)",
            name="calculation_state",
        ),
    )

    detection_key: Mapped[str] = mapped_column(String(512), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("monitoring_run.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounting_snapshot.id", ondelete="RESTRICT"), nullable=False
    )
    rule_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_version.id", ondelete="RESTRICT"), nullable=False
    )
    tax_master_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("tax_master_version.id", ondelete="RESTRICT"), nullable=False
    )
    monitor_type: Mapped[MonitorType] = mapped_column(
        Enum(MonitorType, name="monitor_type"), nullable=False
    )
    calculation_status: Mapped[CalculationStatus] = mapped_column(
        Enum(CalculationStatus, name="calculation_status"), nullable=False
    )
    input_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    result_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    difference_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    rate_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 12))
    tax_burden_rate: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    tax_burden_deviation: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    formula_substitution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    structured_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    not_calculated_reason: Mapped[str | None] = mapped_column(Text)
    alert_code: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str | None] = mapped_column(String(64))


class RiskCase(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "risk_case"
    __table_args__ = (
        CheckConstraint("amount_scale BETWEEN 0 AND 12", name="amount_scale"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority"),
        CheckConstraint("row_version > 0", name="row_version"),
        CheckConstraint(
            "(monitor_type = 'TAX_BURDEN' AND risk_amount IS NULL AND "
            "risk_rate IS NOT NULL) OR "
            "(monitor_type <> 'TAX_BURDEN' AND risk_amount IS NOT NULL AND "
            "risk_rate IS NULL)",
            name="monitor_value",
        ),
        CheckConstraint(
            "risk_amount IS NULL OR risk_amount >= 0",
            name="nonnegative_amount",
        ),
        CheckConstraint(
            "risk_rate IS NULL OR risk_rate >= 0",
            name="nonnegative_rate",
        ),
    )

    fingerprint: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    latest_detection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("detection_record.id", ondelete="RESTRICT")
    )
    monitor_type: Mapped[MonitorType] = mapped_column(
        Enum(MonitorType, name="monitor_type", create_type=False), nullable=False
    )
    status: Mapped[RiskCaseStatus] = mapped_column(
        Enum(RiskCaseStatus, name="risk_case_status"), nullable=False
    )
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    risk_rate: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    risk_direction: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(256))
    merged_into_case_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("risk_case.id", ondelete="RESTRICT")
    )
    lineage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class ReviewAction(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "review_action"
    __table_args__ = (
        CheckConstraint(
            "assignee IS NULL OR (action = 'ASSIGN' AND btrim(assignee) <> '')",
            name="assignee_action",
        ),
        Index("ix_review_action_case_id", "risk_case_id"),
    )

    risk_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_case.id", ondelete="RESTRICT"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[RiskCaseStatus] = mapped_column(
        Enum(RiskCaseStatus, name="risk_case_status", create_type=False), nullable=False
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(256))
    to_status: Mapped[RiskCaseStatus] = mapped_column(
        Enum(RiskCaseStatus, name="risk_case_status", create_type=False), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    correction_voucher_no: Mapped[str | None] = mapped_column(String(128))


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "AuditEvent",
    "CalculationStatus",
    "DetectionRecord",
    "MonitorType",
    "MonitoringRun",
    "MonitoringRunCompany",
    "MonitoringRunCompanyStatus",
    "MonitoringRunStatus",
    "MonitoringRunType",
    "ReviewAction",
    "RiskCase",
    "RiskCaseStatus",
]

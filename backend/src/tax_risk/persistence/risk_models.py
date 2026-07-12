from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
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

from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class MonitoringRunType(StrEnum):
    QUARTERLY = "QUARTERLY"


class MonitoringRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class MonitorType(StrEnum):
    ACCRUAL_ACCURACY = "ACCRUAL_ACCURACY"
    TAX_BURDEN = "TAX_BURDEN"
    POTENTIAL_TAX_COST = "POTENTIAL_TAX_COST"


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
        CheckConstraint("fiscal_year BETWEEN 2000 AND 9999", name="fiscal_year"),
        CheckConstraint("quarter BETWEEN 1 AND 4", name="quarter"),
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
    quarter: Mapped[int] = mapped_column(SmallInteger, nullable=False)
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
            "(calculation_status = 'CALCULATED' AND result_amount IS NOT NULL AND "
            "not_calculated_reason IS NULL) OR "
            "(calculation_status = 'NOT_CALCULABLE' AND result_amount IS NULL AND "
            "not_calculated_reason IS NOT NULL) OR "
            "(calculation_status = 'FAILED' AND result_amount IS NULL AND "
            "not_calculated_reason IS NOT NULL)",
            name="calculation_result_state",
        ),
    )

    detection_key: Mapped[str] = mapped_column(String(512), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("monitoring_run.id", ondelete="CASCADE"), nullable=False, index=True
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
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
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
    __table_args__ = (Index("ix_review_action_case_id", "risk_case_id"),)

    risk_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_case.id", ondelete="RESTRICT"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[RiskCaseStatus] = mapped_column(
        Enum(RiskCaseStatus, name="risk_case_status", create_type=False), nullable=False
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
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
    "MonitoringRunStatus",
    "MonitoringRunType",
    "ReviewAction",
    "RiskCase",
    "RiskCaseStatus",
]

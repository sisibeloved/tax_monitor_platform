from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.domain.exports import ExportJobStatus, ExportType
from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class ExportJob(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "export_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'EXPIRED')",
            name="status",
        ),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="nonnegative_rows"),
        CheckConstraint(
            "checksum IS NULL OR length(checksum) = 64", name="checksum_length"
        ),
        CheckConstraint("length(filters_hash) = 64", name="filters_hash_length"),
        CheckConstraint(
            "length(authorization_version) = 64", name="authorization_version_length"
        ),
        Index("ix_export_job_requester_status", "requester_subject", "status"),
        Index("ix_export_job_expires_at", "expires_at"),
    )

    export_type: Mapped[ExportType] = mapped_column(String(64), nullable=False)
    requester_subject: Mapped[str] = mapped_column(String(256), nullable=False)
    requester_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    company_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    normalized_filters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    filters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ExportJobStatus] = mapped_column(String(32), nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["ExportJob"]

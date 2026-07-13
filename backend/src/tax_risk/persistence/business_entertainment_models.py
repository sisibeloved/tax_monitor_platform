from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.domain.business_entertainment.company_scope import ScopeVersionStatus
from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class BusinessEntertainmentScopeVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_scope_version"
    __table_args__ = (
        CheckConstraint("effective_to >= effective_from", name="effective_period"),
        CheckConstraint("length(file_checksum) = 64", name="file_checksum_length"),
        CheckConstraint(
            "(status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status = 'APPROVED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status IN ('PUBLISHED', 'RETIRED') AND reviewer_id IS NOT NULL "
            "AND approved_at IS NOT NULL AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL)",
            name="status_audit",
        ),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "ingest_batch.id",
            name="fk_be_scope_version_batch",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    source_file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[ScopeVersionStatus] = mapped_column(
        Enum(ScopeVersionStatus, name="business_entertainment_scope_status"),
        nullable=False,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(256))


class BusinessEntertainmentScopeCompany(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_scope_company"
    __table_args__ = (
        UniqueConstraint("version_id", "company_id", name="uq_be_scope_version_company"),
        UniqueConstraint("version_id", "source_record_id", name="uq_be_scope_version_source"),
    )

    version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "business_entertainment_scope_version.id",
            name="fk_be_scope_company_version",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "company.id",
            name="fk_be_scope_company_company",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "source_record.id",
            name="fk_be_scope_company_source",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )


__all__ = ["BusinessEntertainmentScopeCompany", "BusinessEntertainmentScopeVersion"]

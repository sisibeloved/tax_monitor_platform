from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, MetaData, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Canonical declarative registry for every persisted entity."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Database-generated UUID primary key shared by control-plane rows."""

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class AuditTimestampMixin:
    """UTC database audit timestamps for mutable control-plane rows."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ReleaseManifestRecord(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    """Durable release candidate and the hashes needed to reproduce it."""

    __tablename__ = "release_manifest"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CANDIDATE', 'REPLAYING', 'REPLAY_APPROVED', "
            "'REPLAY_REJECTED', 'APPROVED', 'SIGNED', 'VERIFIED', 'PROMOTED')",
            name="status",
        ),
        CheckConstraint("char_length(manifest_sha256) = 64", name="manifest_sha256_length"),
        CheckConstraint(
            "replay_report_sha256 IS NULL OR char_length(replay_report_sha256) = 64",
            name="replay_report_sha256_length",
        ),
    )

    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    replay_report_sha256: Mapped[str | None] = mapped_column(String(64))
    signature_base64: Mapped[str | None] = mapped_column(Text)
    signer_key_id: Mapped[str | None] = mapped_column(String(256))
    signer_key_version: Mapped[str | None] = mapped_column(String(128))
    approvals: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReleaseEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only audit event for every release lifecycle transition."""

    __tablename__ = "release_event"
    __table_args__ = (
        CheckConstraint("char_length(manifest_sha256) = 64", name="manifest_sha256_length"),
        CheckConstraint(
            "report_sha256 IS NULL OR char_length(report_sha256) = 64",
            name="report_sha256_length",
        ),
        CheckConstraint(
            "action IN ('CANDIDATE_CREATED', 'REPLAY_STARTED', 'REPLAY_APPROVED', "
            "'REPLAY_REJECTED', 'RELEASE_APPROVED', 'MANIFEST_SIGNED', "
            "'SIGNATURE_VERIFIED', 'RELEASE_PROMOTED')",
            name="action",
        ),
        Index("ix_release_event_manifest_time", "manifest_id", "occurred_at"),
    )

    manifest_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("release_manifest.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    approver: Mapped[str | None] = mapped_column(String(256))
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = [
    "AuditTimestampMixin",
    "Base",
    "ReleaseEvent",
    "ReleaseManifestRecord",
    "UUIDPrimaryKeyMixin",
]

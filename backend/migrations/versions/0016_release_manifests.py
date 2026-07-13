"""Add signed release manifests and append-only lifecycle events.

Revision ID: 0016_release_manifests
Revises: 0015_delivery_observability
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0016_release_manifests"
down_revision: str | None = "0015_delivery_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_manifest",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_version", sa.String(length=128), nullable=False),
        sa.Column("canonical_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("replay_report_sha256", sa.String(length=64)),
        sa.Column("signature_base64", sa.Text()),
        sa.Column("signer_key_id", sa.String(length=256)),
        sa.Column("signer_key_version", sa.String(length=128)),
        sa.Column(
            "approvals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'REPLAYING', 'REPLAY_APPROVED', "
            "'REPLAY_REJECTED', 'APPROVED', 'SIGNED', 'VERIFIED', 'PROMOTED')",
            name="ck_release_manifest_status",
        ),
        sa.CheckConstraint(
            "char_length(manifest_sha256) = 64",
            name="ck_release_manifest_manifest_sha256_length",
        ),
        sa.CheckConstraint(
            "replay_report_sha256 IS NULL OR char_length(replay_report_sha256) = 64",
            name="ck_release_manifest_replay_report_sha256_length",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_release_manifest"),
        sa.UniqueConstraint(
            "manifest_sha256",
            name="uq_release_manifest_manifest_sha256",
        ),
    )
    op.create_table(
        "release_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("approver", sa.String(length=256)),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("report_sha256", sa.String(length=64)),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('CANDIDATE_CREATED', 'REPLAY_STARTED', 'REPLAY_APPROVED', "
            "'REPLAY_REJECTED', 'RELEASE_APPROVED', 'MANIFEST_SIGNED', "
            "'SIGNATURE_VERIFIED', 'RELEASE_PROMOTED')",
            name="ck_release_event_action",
        ),
        sa.CheckConstraint(
            "char_length(manifest_sha256) = 64",
            name="ck_release_event_manifest_sha256_length",
        ),
        sa.CheckConstraint(
            "report_sha256 IS NULL OR char_length(report_sha256) = 64",
            name="ck_release_event_report_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["release_manifest.id"],
            name="fk_release_event_manifest_id_release_manifest",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_release_event"),
    )
    op.create_index(
        "ix_release_event_manifest_time",
        "release_event",
        ["manifest_id", "occurred_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_release_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'release_event is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_release_event_immutable
        BEFORE UPDATE OR DELETE ON release_event
        FOR EACH ROW EXECUTE FUNCTION reject_release_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_release_event_immutable ON release_event")
    op.execute("DROP FUNCTION IF EXISTS reject_release_event_mutation()")
    op.drop_index("ix_release_event_manifest_time", table_name="release_event")
    op.drop_table("release_event")
    op.drop_table("release_manifest")

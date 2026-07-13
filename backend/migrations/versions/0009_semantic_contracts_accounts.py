"""Add governed shared suggested-account dictionary.

Revision ID: 0009_semantic_accounts
Revises: 0008b_ent_coverage_fields
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0009_semantic_accounts"
down_revision: str | None = "0008b_ent_coverage_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE suggested_account_dictionary_version (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            batch_id uuid NOT NULL,
            dictionary_version varchar(128) NOT NULL,
            effective_from date NOT NULL,
            effective_to date NOT NULL,
            checksum varchar(64) NOT NULL,
            uploaded_by varchar(256) NOT NULL,
            reviewer_id varchar(256),
            published_by varchar(256),
            status varchar(32) NOT NULL,
            approved_at timestamptz,
            published_at timestamptz,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_suggested_account_dictionary_version PRIMARY KEY (id),
            CONSTRAINT fk_account_dict_batch FOREIGN KEY (batch_id)
                REFERENCES ingest_batch(id) ON DELETE RESTRICT,
            CONSTRAINT uq_suggested_account_dictionary_version_batch_id UNIQUE (batch_id),
            CONSTRAINT uq_account_dict_version UNIQUE (dictionary_version),
            CONSTRAINT ck_suggested_account_dictionary_version_effective_period
                CHECK (effective_to >= effective_from),
            CONSTRAINT ck_suggested_account_dictionary_version_status
                CHECK (status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')),
            CONSTRAINT ck_suggested_account_dictionary_version_checksum_length
                CHECK (length(checksum) = 64),
            CONSTRAINT ck_suggested_account_dictionary_version_lifecycle CHECK (
                (status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL
                    AND published_at IS NULL)
                OR (status = 'APPROVED' AND reviewer_id IS NOT NULL
                    AND approved_at IS NOT NULL AND published_at IS NULL)
                OR (status = 'PUBLISHED' AND reviewer_id IS NOT NULL
                    AND approved_at IS NOT NULL AND published_at IS NOT NULL
                    AND published_by IS NOT NULL)
                OR status = 'RETIRED'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE suggested_account_entry (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            dictionary_version_id uuid NOT NULL,
            source_record_id uuid NOT NULL,
            account_id varchar(128) NOT NULL,
            account_code varchar(64) NOT NULL,
            account_name varchar(256) NOT NULL,
            accounting_classification varchar(128) NOT NULL,
            allowed_monitor_types jsonb NOT NULL,
            allowed_labels jsonb NOT NULL,
            status varchar(32) NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_suggested_account_entry PRIMARY KEY (id),
            CONSTRAINT fk_account_entry_version FOREIGN KEY (dictionary_version_id)
                REFERENCES suggested_account_dictionary_version(id) ON DELETE RESTRICT,
            CONSTRAINT fk_account_entry_source FOREIGN KEY (source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT uq_suggested_account_entry_source_record_id UNIQUE (source_record_id),
            CONSTRAINT uq_account_entry_id UNIQUE (dictionary_version_id, account_id),
            CONSTRAINT ck_suggested_account_entry_status
                CHECK (status IN ('ACTIVE', 'INACTIVE'))
        )
        """
    )
    op.create_index(
        "ix_account_entry_version",
        "suggested_account_entry",
        ["dictionary_version_id"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_published_account_dictionary_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'published account dictionary versions are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_account_dictionary_version_immutable
        BEFORE UPDATE OR DELETE ON suggested_account_dictionary_version
        FOR EACH ROW EXECUTE FUNCTION reject_published_account_dictionary_version_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_published_account_entry_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE version_status varchar(32);
        BEGIN
            SELECT status INTO version_status
            FROM suggested_account_dictionary_version
            WHERE id = CASE WHEN TG_OP = 'DELETE'
                THEN OLD.dictionary_version_id ELSE NEW.dictionary_version_id END;
            IF version_status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'published account dictionary entries are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_account_entry_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON suggested_account_entry
        FOR EACH ROW EXECUTE FUNCTION reject_published_account_entry_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_account_entry_immutable ON suggested_account_entry")
    op.execute("DROP FUNCTION IF EXISTS reject_published_account_entry_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_account_dictionary_version_immutable "
        "ON suggested_account_dictionary_version"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_published_account_dictionary_version_mutation()")
    op.drop_index("ix_account_entry_version", table_name="suggested_account_entry")
    op.drop_table("suggested_account_entry")
    op.drop_table("suggested_account_dictionary_version")

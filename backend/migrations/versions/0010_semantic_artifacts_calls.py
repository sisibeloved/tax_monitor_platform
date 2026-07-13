"""Add semantic artifact governance and privacy-safe call audit.

Revision ID: 0010_semantic_artifacts
Revises: 0009_semantic_accounts
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0010_semantic_artifacts"
down_revision: str | None = "0009_semantic_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE semantic_artifact_version (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            artifact_type varchar(32) NOT NULL,
            version varchar(128) NOT NULL,
            checksum varchar(64) NOT NULL,
            storage_ref varchar(512) NOT NULL,
            deployment_id varchar(256),
            effective_from date NOT NULL,
            effective_to date NOT NULL,
            status varchar(32) NOT NULL,
            uploaded_by varchar(256) NOT NULL,
            reviewer_id varchar(256),
            published_by varchar(256),
            approved_at timestamptz,
            published_at timestamptz,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_semantic_artifact_version PRIMARY KEY (id),
            CONSTRAINT uq_semantic_artifact_version UNIQUE (artifact_type, version),
            CONSTRAINT ck_semantic_artifact_version_artifact_type
                CHECK (artifact_type IN ('MODEL', 'PROMPT', 'CASE_LIBRARY')),
            CONSTRAINT ck_semantic_artifact_version_status
                CHECK (status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')),
            CONSTRAINT ck_semantic_artifact_version_effective_period
                CHECK (effective_to >= effective_from),
            CONSTRAINT ck_semantic_artifact_version_checksum_length
                CHECK (length(checksum) = 64),
            CONSTRAINT ck_semantic_artifact_version_lifecycle CHECK (
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
    op.create_index(
        "ix_semantic_artifact_effective",
        "semantic_artifact_version",
        ["artifact_type", "status", "effective_from"],
    )
    op.execute(
        """
        CREATE TABLE semantic_model_call_audit (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            candidate_key varchar(512) NOT NULL,
            company_code varchar(64) NOT NULL,
            model_version_id varchar(128) NOT NULL,
            prompt_version_id varchar(128) NOT NULL,
            case_library_version_id varchar(128) NOT NULL,
            request_checksum varchar(64) NOT NULL,
            output_checksum varchar(64) NOT NULL,
            allowed_fields jsonb NOT NULL,
            token_count integer NOT NULL,
            latency_ms integer NOT NULL,
            schema_status varchar(32) NOT NULL,
            retry_count integer NOT NULL,
            retention_confirmed boolean NOT NULL,
            operator_id varchar(256) NOT NULL,
            run_id varchar(128) NOT NULL,
            occurred_at timestamptz NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_semantic_model_call_audit PRIMARY KEY (id),
            CONSTRAINT ck_semantic_model_call_audit_request_checksum_length
                CHECK (length(request_checksum) = 64),
            CONSTRAINT ck_semantic_model_call_audit_output_checksum_length
                CHECK (length(output_checksum) = 64),
            CONSTRAINT ck_semantic_model_call_audit_nonnegative_tokens CHECK (token_count >= 0),
            CONSTRAINT ck_semantic_model_call_audit_nonnegative_latency CHECK (latency_ms >= 0),
            CONSTRAINT ck_semantic_model_call_audit_nonnegative_retries CHECK (retry_count >= 0)
        )
        """
    )
    op.create_index("ix_model_call_candidate", "semantic_model_call_audit", ["candidate_key"])
    op.create_index("ix_model_call_run", "semantic_model_call_audit", ["run_id"])
    op.execute(
        """
        CREATE FUNCTION reject_published_semantic_artifact_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'published semantic artifacts are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_semantic_artifact_immutable
        BEFORE UPDATE OR DELETE ON semantic_artifact_version
        FOR EACH ROW EXECUTE FUNCTION reject_published_semantic_artifact_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_semantic_call_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'semantic model call audit is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_semantic_call_audit_immutable
        BEFORE UPDATE OR DELETE ON semantic_model_call_audit
        FOR EACH ROW EXECUTE FUNCTION reject_semantic_call_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_semantic_call_audit_immutable "
        "ON semantic_model_call_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_semantic_call_audit_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_semantic_artifact_immutable "
        "ON semantic_artifact_version"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_published_semantic_artifact_mutation()")
    op.drop_index("ix_model_call_run", table_name="semantic_model_call_audit")
    op.drop_index("ix_model_call_candidate", table_name="semantic_model_call_audit")
    op.drop_table("semantic_model_call_audit")
    op.drop_index("ix_semantic_artifact_effective", table_name="semantic_artifact_version")
    op.drop_table("semantic_artifact_version")

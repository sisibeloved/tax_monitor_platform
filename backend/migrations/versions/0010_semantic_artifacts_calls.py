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
        "ALTER TYPE monitor_type ADD VALUE IF NOT EXISTS 'BUSINESS_ENTERTAINMENT'"
    )
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
        CREATE TABLE semantic_detection_record (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            detection_key varchar(512) NOT NULL,
            candidate_key varchar(512) NOT NULL,
            company_code varchar(64) NOT NULL,
            fiscal_year smallint NOT NULL,
            period smallint NOT NULL,
            source_mode varchar(64) NOT NULL,
            canonical_source_record_id uuid NOT NULL,
            sap_observation_id uuid,
            sap_document_number varchar(64),
            sap_line_item varchar(32),
            amount numeric(38, 12) NOT NULL,
            currency varchar(3) NOT NULL,
            snapshot_id uuid NOT NULL,
            exact_evidence_link_id uuid,
            rule_version_id varchar(128) NOT NULL,
            model_version_id varchar(128) NOT NULL,
            prompt_version_id varchar(128) NOT NULL,
            case_library_version_id varchar(128) NOT NULL,
            account_dictionary_version varchar(128) NOT NULL,
            semantic_label varchar(64) NOT NULL,
            confidence_tier varchar(32) NOT NULL,
            evidence_refs jsonb NOT NULL,
            recommended_account_ids jsonb NOT NULL,
            rationale_summary text NOT NULL,
            missing_evidence jsonb NOT NULL,
            detected_at timestamptz NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_semantic_detection_record PRIMARY KEY (id),
            CONSTRAINT uq_semantic_detection_key UNIQUE (detection_key),
            CONSTRAINT fk_sem_detection_source FOREIGN KEY (canonical_source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sem_detection_sap FOREIGN KEY (sap_observation_id)
                REFERENCES sap_expense_voucher_observation(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sem_detection_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sem_detection_evidence_link FOREIGN KEY (exact_evidence_link_id)
                REFERENCES evidence_link(id) ON DELETE RESTRICT,
            CONSTRAINT ck_semantic_detection_record_fiscal_year
                CHECK (fiscal_year BETWEEN 2000 AND 9999),
            CONSTRAINT ck_semantic_detection_record_period CHECK (period BETWEEN 1 AND 12),
            CONSTRAINT ck_semantic_detection_record_currency CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_semantic_detection_record_source_mode
                CHECK (source_mode IN ('SAP_LINKED', 'BUSINESS_DOCUMENT_UNLINKED')),
            CONSTRAINT ck_semantic_detection_record_sap_identity CHECK (
                (source_mode = 'SAP_LINKED' AND sap_observation_id IS NOT NULL
                    AND sap_document_number IS NOT NULL AND sap_line_item IS NOT NULL)
                OR (source_mode = 'BUSINESS_DOCUMENT_UNLINKED'
                    AND sap_observation_id IS NULL AND sap_document_number IS NULL
                    AND sap_line_item IS NULL)
            )
        )
        """
    )
    op.create_index(
        "ix_semantic_detection_candidate",
        "semantic_detection_record",
        ["candidate_key"],
    )
    op.create_index(
        "ix_semantic_detection_company_period",
        "semantic_detection_record",
        ["company_code", "fiscal_year", "period"],
    )
    op.execute(
        """
        CREATE TABLE semantic_evidence_task (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            detection_id uuid NOT NULL,
            company_code varchar(64) NOT NULL,
            status varchar(32) NOT NULL,
            missing_evidence jsonb NOT NULL,
            reason text NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_semantic_evidence_task PRIMARY KEY (id),
            CONSTRAINT uq_semantic_evidence_task_detection UNIQUE (detection_id),
            CONSTRAINT fk_sem_evidence_task_detection FOREIGN KEY (detection_id)
                REFERENCES semantic_detection_record(id) ON DELETE RESTRICT,
            CONSTRAINT ck_semantic_evidence_task_status
                CHECK (status IN ('OPEN', 'COMPLETED', 'CANCELLED'))
        )
        """
    )
    op.create_index(
        "ix_semantic_evidence_task_company",
        "semantic_evidence_task",
        ["company_code", "status"],
    )
    op.execute(
        """
        CREATE TABLE business_entertainment_case_detail (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            risk_case_id uuid NOT NULL,
            semantic_detection_id uuid NOT NULL,
            candidate_key varchar(512) NOT NULL,
            canonical_source_record_id uuid NOT NULL,
            source_mode varchar(64) NOT NULL,
            sap_link_status varchar(32) NOT NULL,
            sap_observation_id uuid,
            risk_amount_source varchar(64) NOT NULL,
            confidence_tier varchar(32) NOT NULL,
            account_dictionary_version varchar(128) NOT NULL,
            exact_evidence_link_id uuid,
            workflow_note text NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_business_entertainment_case_detail PRIMARY KEY (id),
            CONSTRAINT uq_be_case_detail_case UNIQUE (risk_case_id),
            CONSTRAINT fk_be_case_detail_case FOREIGN KEY (risk_case_id)
                REFERENCES risk_case(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_case_detail_detection FOREIGN KEY (semantic_detection_id)
                REFERENCES semantic_detection_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_case_detail_source FOREIGN KEY (canonical_source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_case_detail_sap FOREIGN KEY (sap_observation_id)
                REFERENCES sap_expense_voucher_observation(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_case_detail_link FOREIGN KEY (exact_evidence_link_id)
                REFERENCES evidence_link(id) ON DELETE RESTRICT,
            CONSTRAINT ck_business_entertainment_case_detail_source_mode
                CHECK (source_mode IN ('SAP_LINKED', 'BUSINESS_DOCUMENT_UNLINKED')),
            CONSTRAINT ck_business_entertainment_case_detail_sap_link_status
                CHECK (sap_link_status IN ('LINKED', 'PENDING_LOCATION'))
        )
        """
    )
    op.create_index(
        "ix_be_case_detail_detection",
        "business_entertainment_case_detail",
        ["semantic_detection_id"],
    )
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
    op.execute(
        """
        CREATE TRIGGER trg_semantic_detection_immutable
        BEFORE UPDATE OR DELETE ON semantic_detection_record
        FOR EACH ROW EXECUTE FUNCTION reject_semantic_call_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_semantic_detection_immutable "
        "ON semantic_detection_record"
    )
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
    op.drop_index(
        "ix_be_case_detail_detection",
        table_name="business_entertainment_case_detail",
    )
    op.drop_table("business_entertainment_case_detail")
    op.drop_index(
        "ix_semantic_evidence_task_company",
        table_name="semantic_evidence_task",
    )
    op.drop_table("semantic_evidence_task")
    op.drop_index(
        "ix_semantic_detection_company_period",
        table_name="semantic_detection_record",
    )
    op.drop_index(
        "ix_semantic_detection_candidate",
        table_name="semantic_detection_record",
    )
    op.drop_table("semantic_detection_record")
    op.drop_index("ix_semantic_artifact_effective", table_name="semantic_artifact_version")
    op.drop_table("semantic_artifact_version")

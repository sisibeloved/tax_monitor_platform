"""Add immutable business-entertainment evidence observations.

Revision ID: 0008_entertainment_observations
Revises: 0007_entertainment_scope
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0008_entertainment_observations"
down_revision: str | None = "0007_entertainment_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_record ALTER COLUMN amount DROP NOT NULL")
    op.create_check_constraint(
        op.f("ck_source_record_amount_required_by_dataset"),
        "source_record",
        "amount IS NOT NULL OR dataset_code = 'oa_material_requisition'",
    )
    op.execute(
        """
        CREATE TABLE sap_expense_voucher_observation (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            source_record_id uuid NOT NULL,
            ingest_batch_id uuid NOT NULL,
            source_record_key varchar(512) NOT NULL,
            company_code varchar(64) NOT NULL,
            fiscal_year smallint NOT NULL,
            period smallint NOT NULL,
            posting_date date NOT NULL,
            document_number varchar(64) NOT NULL,
            line_item varchar(32) NOT NULL,
            current_account_code varchar(64) NOT NULL,
            current_account_name varchar(256) NOT NULL,
            amount numeric(38, 12) NOT NULL,
            currency varchar(3) NOT NULL,
            summary text NOT NULL,
            assignment varchar(256),
            reference varchar(256),
            reversal_reference varchar(256),
            account_family varchar(64) NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_sap_expense_voucher_observation PRIMARY KEY (id),
            CONSTRAINT fk_sap_obs_source FOREIGN KEY (source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sap_obs_batch FOREIGN KEY (ingest_batch_id)
                REFERENCES ingest_batch(id) ON DELETE RESTRICT,
            CONSTRAINT uq_sap_expense_voucher_observation_source_record_id
                UNIQUE (source_record_id),
            CONSTRAINT uq_sap_obs_batch_key
                UNIQUE (ingest_batch_id, source_record_key),
            CONSTRAINT uq_sap_obs_batch_business_key
                UNIQUE (ingest_batch_id, company_code, fiscal_year, document_number, line_item),
            CONSTRAINT ck_sap_expense_voucher_observation_period
                CHECK (period BETWEEN 1 AND 12),
            CONSTRAINT ck_sap_expense_voucher_observation_currency
                CHECK (currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_sap_expense_voucher_observation_account_family
                CHECK (account_family = 'BUSINESS_ENTERTAINMENT')
        )
        """
    )
    op.create_index("ix_sap_obs_batch", "sap_expense_voucher_observation", ["ingest_batch_id"])
    op.create_index("ix_sap_obs_company", "sap_expense_voucher_observation", ["company_code"])

    op.execute(
        """
        CREATE TABLE sap_expense_voucher_snapshot_projection (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            observation_id uuid NOT NULL,
            snapshot_id uuid NOT NULL,
            company_code varchar(64) NOT NULL,
            period date NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_sap_expense_voucher_snapshot_projection PRIMARY KEY (id),
            CONSTRAINT fk_sap_projection_obs FOREIGN KEY (observation_id)
                REFERENCES sap_expense_voucher_observation(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sap_projection_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
            CONSTRAINT uq_sap_projection_snapshot_obs UNIQUE (snapshot_id, observation_id)
        )
        """
    )
    op.create_index(
        "ix_sap_projection_obs",
        "sap_expense_voucher_snapshot_projection",
        ["observation_id"],
    )
    op.create_index(
        "ix_sap_projection_snapshot",
        "sap_expense_voucher_snapshot_projection",
        ["snapshot_id"],
    )

    op.execute(
        """
        CREATE TABLE business_entertainment_source_observation (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            source_record_id uuid NOT NULL,
            ingest_batch_id uuid NOT NULL,
            dataset_code varchar(128) NOT NULL,
            source_record_key varchar(512) NOT NULL,
            company_code varchar(64) NOT NULL,
            fiscal_year smallint NOT NULL,
            period smallint NOT NULL,
            document_date date NOT NULL,
            document_id varchar(128) NOT NULL,
            line_id varchar(64) NOT NULL,
            amount numeric(38, 12),
            currency varchar(3),
            parent_oa_id varchar(128),
            parent_hesi_id varchar(128),
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_business_entertainment_source_observation PRIMARY KEY (id),
            CONSTRAINT fk_be_source_obs_source FOREIGN KEY (source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_source_obs_batch FOREIGN KEY (ingest_batch_id)
                REFERENCES ingest_batch(id) ON DELETE RESTRICT,
            CONSTRAINT uq_business_entertainment_source_observation_source_record_id
                UNIQUE (source_record_id),
            CONSTRAINT uq_be_source_batch_key UNIQUE (ingest_batch_id, source_record_key),
            CONSTRAINT ck_business_entertainment_source_observation_period
                CHECK (period BETWEEN 1 AND 12),
            CONSTRAINT ck_business_entertainment_source_observation_currency
                CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
            CONSTRAINT ck_business_entertainment_source_observation_amount_currency_pair
                CHECK ((amount IS NULL AND currency IS NULL)
                    OR (amount IS NOT NULL AND currency IS NOT NULL))
        )
        """
    )
    op.create_index(
        "ix_be_source_obs_batch",
        "business_entertainment_source_observation",
        ["ingest_batch_id"],
    )
    op.create_index(
        "ix_be_source_obs_company",
        "business_entertainment_source_observation",
        ["company_code"],
    )

    op.execute(
        """
        CREATE TABLE evidence_link (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            company_code varchar(64) NOT NULL,
            source_record_id uuid NOT NULL,
            target_record_id uuid NOT NULL,
            relation_kind varchar(64) NOT NULL,
            relation_quality varchar(32) NOT NULL,
            matched_field varchar(128) NOT NULL,
            snapshot_id uuid NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_evidence_link PRIMARY KEY (id),
            CONSTRAINT fk_evidence_source FOREIGN KEY (source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_evidence_target FOREIGN KEY (target_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_evidence_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
            CONSTRAINT uq_evidence_link_snapshot_relation
                UNIQUE (snapshot_id, source_record_id, target_record_id, relation_kind)
        )
        """
    )
    op.create_index("ix_evidence_link_company", "evidence_link", ["company_code"])
    op.create_index("ix_evidence_link_snapshot", "evidence_link", ["snapshot_id"])

    op.execute(
        """
        CREATE TABLE business_entertainment_evaluation (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            candidate_key varchar(512) NOT NULL,
            company_code varchar(64) NOT NULL,
            fiscal_year smallint NOT NULL,
            period smallint NOT NULL,
            source_mode varchar(64) NOT NULL,
            canonical_record_type varchar(64) NOT NULL,
            canonical_source_record_id uuid NOT NULL,
            sap_observation_id uuid,
            amount numeric(38, 12),
            amount_source varchar(64) NOT NULL,
            snapshot_id uuid NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_business_entertainment_evaluation PRIMARY KEY (id),
            CONSTRAINT fk_be_eval_source FOREIGN KEY (canonical_source_record_id)
                REFERENCES source_record(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_eval_sap_obs FOREIGN KEY (sap_observation_id)
                REFERENCES sap_expense_voucher_observation(id) ON DELETE RESTRICT,
            CONSTRAINT fk_be_eval_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
            CONSTRAINT uq_be_evaluation_snapshot_key UNIQUE (snapshot_id, candidate_key),
            CONSTRAINT ck_business_entertainment_evaluation_period
                CHECK (period BETWEEN 1 AND 12)
        )
        """
    )
    op.create_index(
        "ix_be_evaluation_company",
        "business_entertainment_evaluation",
        ["company_code"],
    )
    op.create_index(
        "ix_be_evaluation_snapshot",
        "business_entertainment_evaluation",
        ["snapshot_id"],
    )

    op.execute(
        """
        CREATE TABLE sap_link_coverage (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            company_code varchar(64) NOT NULL,
            period date NOT NULL,
            sap_observation_id uuid NOT NULL,
            link_status varchar(64) NOT NULL,
            exact_evidence_link_id uuid,
            evaluated_via_business_document boolean NOT NULL,
            snapshot_id uuid NOT NULL,
            created_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at timestamptz DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_sap_link_coverage PRIMARY KEY (id),
            CONSTRAINT fk_sap_coverage_obs FOREIGN KEY (sap_observation_id)
                REFERENCES sap_expense_voucher_observation(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sap_coverage_link FOREIGN KEY (exact_evidence_link_id)
                REFERENCES evidence_link(id) ON DELETE RESTRICT,
            CONSTRAINT fk_sap_coverage_snapshot FOREIGN KEY (snapshot_id)
                REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
            CONSTRAINT uq_sap_coverage_snapshot_obs
                UNIQUE (snapshot_id, sap_observation_id)
        )
        """
    )
    op.create_index("ix_sap_coverage_company", "sap_link_coverage", ["company_code"])
    op.create_index("ix_sap_coverage_snapshot", "sap_link_coverage", ["snapshot_id"])

    op.execute(
        """
        CREATE FUNCTION reject_immutable_entertainment_observation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable_business_entertainment_observation';
        END;
        $$
        """
    )
    for table_name in (
        "sap_expense_voucher_observation",
        "sap_expense_voucher_snapshot_projection",
        "business_entertainment_source_observation",
    ):
        op.execute(
            f"CREATE TRIGGER trg_immutable_{table_name} "
            f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION reject_immutable_entertainment_observation()"
        )


def downgrade() -> None:
    for table_name in (
        "business_entertainment_source_observation",
        "sap_expense_voucher_snapshot_projection",
        "sap_expense_voucher_observation",
    ):
        op.execute(f"DROP TRIGGER trg_immutable_{table_name} ON {table_name}")
    op.execute("DROP FUNCTION reject_immutable_entertainment_observation()")
    op.drop_table("sap_link_coverage")
    op.drop_table("business_entertainment_evaluation")
    op.drop_table("evidence_link")
    op.drop_table("business_entertainment_source_observation")
    op.drop_table("sap_expense_voucher_snapshot_projection")
    op.drop_table("sap_expense_voucher_observation")
    op.execute(
        """
        DELETE FROM source_record
        WHERE batch_id IN (
            SELECT id FROM ingest_batch
            WHERE dataset_code IN (
                'sap_business_entertainment',
                'hesi_business_entertainment',
                'oa_business_entertainment',
                'oa_self_procurement',
                'oa_material_requisition'
            )
        )
        """
    )
    op.execute(
        """
        DELETE FROM ingest_batch
        WHERE dataset_code IN (
            'sap_business_entertainment',
            'hesi_business_entertainment',
            'oa_business_entertainment',
            'oa_self_procurement',
            'oa_material_requisition'
        )
        """
    )
    op.execute(
        "ALTER TABLE source_record DROP CONSTRAINT IF EXISTS "
        "ck_source_record_amount_required_by_dataset"
    )
    op.execute("ALTER TABLE source_record ALTER COLUMN amount SET NOT NULL")

"""Create the auditable control-plane schema.

Revision ID: 0001_control_plane
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0001_control_plane"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("company_lifecycle", ("ACTIVE", "INACTIVE")),
    (
        "ingest_batch_status",
        ("RECEIVED", "VALIDATING", "SUCCEEDED", "PARTIAL", "FAILED"),
    ),
    ("ingest_mode", ("FULL", "INCREMENTAL")),
    ("version_status", ("DRAFT", "PUBLISHED", "RETIRED")),
    ("snapshot_status", ("DRAFT", "VALIDATED", "PUBLISHED")),
    ("snapshot_set_status", ("DRAFT", "VALIDATED", "PUBLISHED")),
    ("monitoring_run_type", ("QUARTERLY",)),
    (
        "monitoring_run_status",
        ("PENDING", "RUNNING", "PARTIAL_SUCCESS", "SUCCEEDED", "FAILED"),
    ),
    ("monitor_type", ("ACCRUAL_ACCURACY", "TAX_BURDEN", "POTENTIAL_TAX_COST")),
    ("calculation_status", ("CALCULATED", "NOT_CALCULABLE", "FAILED")),
    (
        "risk_case_status",
        (
            "NEW",
            "ASSIGNED",
            "PENDING_COMPANY_CONFIRMATION",
            "PENDING_ADJUSTMENT",
            "ADJUSTED_PENDING_REVIEW",
            "CLOSED",
            "GROUP_REVIEW",
            "EVIDENCE_REQUIRED",
        ),
    ),
)


TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE company (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_code VARCHAR(64) NOT NULL,
        company_name VARCHAR(256) NOT NULL,
        lifecycle company_lifecycle NOT NULL DEFAULT 'ACTIVE',
        lifecycle_changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        deactivated_at TIMESTAMPTZ,
        lifecycle_reason TEXT,
        lifecycle_changed_by VARCHAR(256),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_company_company_code UNIQUE (company_code),
        CONSTRAINT ck_company_lifecycle_audit CHECK (
            (lifecycle = 'ACTIVE' AND deactivated_at IS NULL)
            OR (lifecycle = 'INACTIVE' AND deactivated_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE ingest_batch (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source VARCHAR(64) NOT NULL,
        source_batch_key VARCHAR(256) NOT NULL,
        dataset_code VARCHAR(128) NOT NULL,
        status ingest_batch_status NOT NULL,
        extraction_time TIMESTAMPTZ NOT NULL,
        period DATE NOT NULL,
        mode ingest_mode NOT NULL,
        schema_version VARCHAR(64) NOT NULL,
        payload_ref TEXT,
        source_primary_key_definition JSONB NOT NULL DEFAULT '{}'::jsonb,
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        record_count INTEGER NOT NULL,
        accepted_count INTEGER NOT NULL,
        rejected_count INTEGER NOT NULL,
        control_total NUMERIC(38, 12) NOT NULL,
        checksum VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_ingest_batch_source_key UNIQUE (source, source_batch_key),
        CONSTRAINT ck_ingest_batch_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_ingest_batch_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_ingest_batch_nonnegative_counts CHECK (
            record_count >= 0 AND accepted_count >= 0 AND rejected_count >= 0
        ),
        CONSTRAINT ck_ingest_batch_reconciled_counts CHECK (
            accepted_count + rejected_count = record_count
        ),
        CONSTRAINT ck_ingest_batch_checksum_length CHECK (length(checksum) = 64)
    )
    """,
    """
    CREATE TABLE ingest_error (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        batch_id UUID NOT NULL REFERENCES ingest_batch(id) ON DELETE CASCADE,
        row_number INTEGER NOT NULL,
        error_code VARCHAR(128) NOT NULL,
        message TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        retryable BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT ck_ingest_error_positive_row_number CHECK (row_number > 0)
    )
    """,
    """
    CREATE TABLE source_record (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        batch_id UUID NOT NULL REFERENCES ingest_batch(id) ON DELETE RESTRICT,
        source_record_key VARCHAR(512) NOT NULL,
        company_id UUID REFERENCES company(id) ON DELETE RESTRICT,
        dataset_code VARCHAR(128) NOT NULL,
        period DATE NOT NULL,
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        amount NUMERIC(38, 12) NOT NULL,
        payload JSONB NOT NULL,
        lineage JSONB NOT NULL,
        extracted_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_source_record_batch_key UNIQUE (batch_id, source_record_key),
        CONSTRAINT ck_source_record_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_source_record_currency CHECK (currency ~ '^[A-Z]{3}$')
    )
    """,
    """
    CREATE TABLE tax_master_version (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        source_batch_id UUID NOT NULL REFERENCES ingest_batch(id) ON DELETE RESTRICT,
        valid_from DATE NOT NULL,
        valid_to DATE,
        version VARCHAR(64) NOT NULL,
        status version_status NOT NULL,
        tax_rate NUMERIC(20, 12) NOT NULL,
        loss_carryforward NUMERIC(38, 12) NOT NULL,
        average_tax_burden_rate_3y NUMERIC(20, 12) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        source_file_name TEXT,
        source_checksum VARCHAR(64),
        data JSONB NOT NULL,
        published_at TIMESTAMPTZ,
        approved_by VARCHAR(256),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_tax_master_id_company UNIQUE (id, company_id),
        CONSTRAINT uq_tax_master_company_effective_version
            UNIQUE (company_id, valid_from, version),
        CONSTRAINT ck_tax_master_valid_period CHECK (valid_to IS NULL OR valid_to >= valid_from),
        CONSTRAINT ck_tax_master_tax_rate CHECK (tax_rate BETWEEN 0 AND 1),
        CONSTRAINT ck_tax_master_average_rate CHECK (
            average_tax_burden_rate_3y BETWEEN 0 AND 1
        ),
        CONSTRAINT ck_tax_master_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_tax_master_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_tax_master_published_at_state CHECK (
            (status = 'DRAFT' AND published_at IS NULL)
            OR (status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE rule_version (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        rule_code VARCHAR(128) NOT NULL,
        version VARCHAR(64) NOT NULL,
        status version_status NOT NULL,
        effective_from DATE NOT NULL,
        effective_to DATE,
        definition JSONB NOT NULL,
        change_reason TEXT NOT NULL,
        published_at TIMESTAMPTZ,
        approved_by VARCHAR(256),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_rule_version_code_version UNIQUE (rule_code, version),
        CONSTRAINT ck_rule_version_valid_period CHECK (
            effective_to IS NULL OR effective_to >= effective_from
        ),
        CONSTRAINT ck_rule_version_published_at_state CHECK (
            (status = 'DRAFT' AND published_at IS NULL)
            OR (status IN ('PUBLISHED', 'RETIRED') AND published_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE accounting_snapshot (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        tax_master_version_id UUID NOT NULL,
        period DATE NOT NULL,
        source_version_set_hash VARCHAR(64) NOT NULL,
        status snapshot_status NOT NULL DEFAULT 'DRAFT',
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        record_count INTEGER NOT NULL,
        control_total NUMERIC(38, 12) NOT NULL,
        checksum VARCHAR(64) NOT NULL,
        lineage JSONB NOT NULL DEFAULT '{}'::jsonb,
        published_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_accounting_snapshot_master_company
            FOREIGN KEY (tax_master_version_id, company_id)
            REFERENCES tax_master_version(id, company_id) ON DELETE RESTRICT,
        CONSTRAINT uq_accounting_snapshot_id_company_master
            UNIQUE (id, company_id, tax_master_version_id),
        CONSTRAINT uq_accounting_snapshot_company_period_sources
            UNIQUE (company_id, period, source_version_set_hash),
        CONSTRAINT ck_accounting_snapshot_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_accounting_snapshot_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_accounting_snapshot_record_count CHECK (record_count >= 0),
        CONSTRAINT ck_accounting_snapshot_source_hash CHECK (
            length(source_version_set_hash) = 64
        ),
        CONSTRAINT ck_accounting_snapshot_checksum CHECK (length(checksum) = 64),
        CONSTRAINT ck_accounting_snapshot_published_at_state CHECK (
            (status IN ('DRAFT', 'VALIDATED') AND published_at IS NULL)
            OR (status = 'PUBLISHED' AND published_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE snapshot_source (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        snapshot_id UUID NOT NULL REFERENCES accounting_snapshot(id) ON DELETE CASCADE,
        ingest_batch_id UUID NOT NULL REFERENCES ingest_batch(id) ON DELETE RESTRICT,
        source VARCHAR(64) NOT NULL,
        source_version VARCHAR(128) NOT NULL,
        record_count INTEGER NOT NULL,
        control_total NUMERIC(38, 12) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        lineage JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_snapshot_source_snapshot_batch UNIQUE (snapshot_id, ingest_batch_id),
        CONSTRAINT ck_snapshot_source_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_snapshot_source_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_snapshot_source_record_count CHECK (record_count >= 0)
    )
    """,
    """
    CREATE TABLE snapshot_set (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        set_key VARCHAR(256) NOT NULL,
        period DATE NOT NULL,
        status snapshot_set_status NOT NULL DEFAULT 'DRAFT',
        expected_member_count INTEGER NOT NULL,
        published_at TIMESTAMPTZ,
        supersedes_snapshot_set_id UUID REFERENCES snapshot_set(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_snapshot_set_set_key UNIQUE (set_key),
        CONSTRAINT ck_snapshot_set_minimum_member_count CHECK (expected_member_count >= 100),
        CONSTRAINT ck_snapshot_set_published_at_state CHECK (
            (status = 'PUBLISHED' AND published_at IS NOT NULL)
            OR (status <> 'PUBLISHED' AND published_at IS NULL)
        )
    )
    """,
    """
    CREATE TABLE snapshot_set_member (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        snapshot_set_id UUID NOT NULL REFERENCES snapshot_set(id) ON DELETE CASCADE,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        snapshot_id UUID NOT NULL REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_snapshot_set_member_set_company UNIQUE (snapshot_set_id, company_id),
        CONSTRAINT uq_snapshot_set_member_set_snapshot UNIQUE (snapshot_set_id, snapshot_id)
    )
    """,
    """
    CREATE TABLE monitoring_run (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        run_key VARCHAR(512) NOT NULL,
        run_type monitoring_run_type NOT NULL,
        snapshot_set_id UUID NOT NULL REFERENCES snapshot_set(id) ON DELETE RESTRICT,
        rule_version_id UUID NOT NULL REFERENCES rule_version(id) ON DELETE RESTRICT,
        status monitoring_run_status NOT NULL,
        fiscal_year INTEGER NOT NULL,
        quarter SMALLINT NOT NULL,
        requested_company_count INTEGER NOT NULL,
        succeeded_company_count INTEGER NOT NULL DEFAULT 0,
        failed_company_count INTEGER NOT NULL DEFAULT 0,
        blocked_company_count INTEGER NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        failure_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_monitoring_run_key UNIQUE (run_key),
        CONSTRAINT ck_monitoring_run_fiscal_year CHECK (fiscal_year BETWEEN 2000 AND 9999),
        CONSTRAINT ck_monitoring_run_quarter CHECK (quarter BETWEEN 1 AND 4),
        CONSTRAINT ck_monitoring_run_nonnegative_counts CHECK (
            requested_company_count >= 0 AND succeeded_company_count >= 0
            AND failed_company_count >= 0 AND blocked_company_count >= 0
        )
    )
    """,
    """
    CREATE TABLE detection_record (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        detection_key VARCHAR(512) NOT NULL,
        run_id UUID NOT NULL REFERENCES monitoring_run(id) ON DELETE CASCADE,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        snapshot_id UUID NOT NULL REFERENCES accounting_snapshot(id) ON DELETE RESTRICT,
        rule_version_id UUID NOT NULL REFERENCES rule_version(id) ON DELETE RESTRICT,
        tax_master_version_id UUID NOT NULL REFERENCES tax_master_version(id) ON DELETE RESTRICT,
        monitor_type monitor_type NOT NULL,
        calculation_status calculation_status NOT NULL,
        input_amount NUMERIC(38, 12),
        result_amount NUMERIC(38, 12),
        difference_amount NUMERIC(38, 12),
        rate_value NUMERIC(20, 12),
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        formula_substitution JSONB NOT NULL,
        lineage JSONB NOT NULL,
        structured_output JSONB NOT NULL,
        not_calculated_reason TEXT,
        alert_code VARCHAR(128),
        direction VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_detection_snapshot_company_master
            FOREIGN KEY (snapshot_id, company_id, tax_master_version_id)
            REFERENCES accounting_snapshot(id, company_id, tax_master_version_id)
            ON DELETE RESTRICT,
        CONSTRAINT uq_detection_record_key UNIQUE (detection_key),
        CONSTRAINT ck_detection_record_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_detection_record_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_detection_record_rate CHECK (
            rate_value IS NULL OR rate_value BETWEEN 0 AND 1
        ),
        CONSTRAINT ck_detection_record_calculation_state CHECK (
            (
                calculation_status = 'CALCULATED'
                AND result_amount IS NOT NULL
                AND not_calculated_reason IS NULL
            ) OR (
                calculation_status = 'NOT_CALCULABLE'
                AND result_amount IS NULL
                AND not_calculated_reason IS NOT NULL
            ) OR (
                calculation_status = 'FAILED'
                AND result_amount IS NULL
                AND not_calculated_reason IS NOT NULL
            )
        )
    )
    """,
    """
    CREATE TABLE risk_case (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        fingerprint VARCHAR(512) NOT NULL,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        latest_detection_id UUID REFERENCES detection_record(id) ON DELETE RESTRICT,
        monitor_type monitor_type NOT NULL,
        status risk_case_status NOT NULL,
        risk_amount NUMERIC(38, 12) NOT NULL,
        currency VARCHAR(3) NOT NULL,
        amount_scale SMALLINT NOT NULL,
        risk_direction VARCHAR(64) NOT NULL,
        priority SMALLINT NOT NULL,
        assignee VARCHAR(256),
        merged_into_case_id UUID REFERENCES risk_case(id) ON DELETE RESTRICT,
        lineage JSONB NOT NULL DEFAULT '{}'::jsonb,
        row_version INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_risk_case_fingerprint UNIQUE (fingerprint),
        CONSTRAINT ck_risk_case_amount_scale CHECK (amount_scale BETWEEN 0 AND 12),
        CONSTRAINT ck_risk_case_currency CHECK (currency ~ '^[A-Z]{3}$'),
        CONSTRAINT ck_risk_case_priority CHECK (priority BETWEEN 1 AND 5),
        CONSTRAINT ck_risk_case_row_version CHECK (row_version > 0)
    )
    """,
    """
    CREATE TABLE review_action (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        risk_case_id UUID NOT NULL REFERENCES risk_case(id) ON DELETE RESTRICT,
        actor VARCHAR(256) NOT NULL,
        actor_role VARCHAR(128) NOT NULL,
        from_status risk_case_status NOT NULL,
        action VARCHAR(128) NOT NULL,
        to_status risk_case_status NOT NULL,
        reason TEXT NOT NULL,
        attachment_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
        correction_voucher_no VARCHAR(128),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE audit_event (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        entity_type VARCHAR(128) NOT NULL,
        entity_id UUID NOT NULL,
        action VARCHAR(128) NOT NULL,
        actor VARCHAR(256) NOT NULL,
        correlation_id UUID,
        payload JSONB NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX ix_ingest_error_batch_id ON ingest_error(batch_id)",
    "CREATE INDEX ix_source_record_batch_id ON source_record(batch_id)",
    "CREATE INDEX ix_source_record_company_id ON source_record(company_id)",
    "CREATE INDEX ix_tax_master_version_company_id ON tax_master_version(company_id)",
    "CREATE INDEX ix_accounting_snapshot_company_id ON accounting_snapshot(company_id)",
    "CREATE INDEX ix_snapshot_source_snapshot_id ON snapshot_source(snapshot_id)",
    "CREATE INDEX ix_snapshot_set_member_set_id ON snapshot_set_member(snapshot_set_id)",
    "CREATE INDEX ix_detection_record_run_id ON detection_record(run_id)",
    "CREATE INDEX ix_risk_case_company_id ON risk_case(company_id)",
    "CREATE INDEX ix_review_action_case_id ON review_action(risk_case_id)",
    "CREATE INDEX ix_audit_event_entity ON audit_event(entity_type, entity_id)",
    "CREATE INDEX ix_audit_event_correlation_id ON audit_event(correlation_id)",
)


TRIGGER_DDL: tuple[str, ...] = (
    """
    CREATE FUNCTION reject_published_accounting_snapshot_change()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status = 'PUBLISHED' THEN
            RAISE EXCEPTION 'immutable_snapshot: accounting_snapshot % is published', OLD.id
                USING ERRCODE = '55000';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_accounting_snapshot_immutable
    BEFORE UPDATE OR DELETE ON accounting_snapshot
    FOR EACH ROW EXECUTE FUNCTION reject_published_accounting_snapshot_change()
    """,
    """
    CREATE FUNCTION reject_published_snapshot_source_change()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        row_id UUID;
        old_parent_id UUID;
        new_parent_id UUID;
        parent_row RECORD;
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            row_id := OLD.id;
            old_parent_id := OLD.snapshot_id;
        END IF;

        IF TG_OP <> 'DELETE' THEN
            row_id := NEW.id;
            new_parent_id := NEW.snapshot_id;
        END IF;

        FOR parent_row IN
            SELECT id, status
            FROM accounting_snapshot
            WHERE id = old_parent_id OR id = new_parent_id
            ORDER BY id
            FOR UPDATE
        LOOP
            IF parent_row.status = 'PUBLISHED' THEN
                RAISE EXCEPTION
                    'immutable_snapshot: snapshot_source % belongs to published snapshot %',
                    row_id, parent_row.id USING ERRCODE = '55000';
            END IF;
        END LOOP;

        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_source_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON snapshot_source
    FOR EACH ROW EXECUTE FUNCTION reject_published_snapshot_source_change()
    """,
    """
    CREATE FUNCTION assign_snapshot_set_published_at()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status = 'PUBLISHED' THEN
                NEW.published_at := clock_timestamp();
            END IF;
        ELSIF NEW.status = 'PUBLISHED' AND OLD.status <> 'PUBLISHED' THEN
            NEW.published_at := clock_timestamp();
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_set_publish_time
    BEFORE INSERT OR UPDATE ON snapshot_set
    FOR EACH ROW EXECUTE FUNCTION assign_snapshot_set_published_at()
    """,
    """
    CREATE FUNCTION validate_snapshot_set_completeness()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        is_publication BOOLEAN := false;
        actual_member_count INTEGER;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            is_publication := NEW.status = 'PUBLISHED';
        ELSE
            is_publication := NEW.status = 'PUBLISHED' AND OLD.status <> 'PUBLISHED';
        END IF;

        IF is_publication THEN
            SELECT count(*) INTO actual_member_count
            FROM snapshot_set_member
            WHERE snapshot_set_id = NEW.id;

            IF actual_member_count <> NEW.expected_member_count THEN
                RAISE EXCEPTION
                    'incomplete_snapshot_set: expected % members, found %',
                    NEW.expected_member_count, actual_member_count
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_set_complete
    BEFORE INSERT OR UPDATE ON snapshot_set
    FOR EACH ROW EXECUTE FUNCTION validate_snapshot_set_completeness()
    """,
    """
    CREATE FUNCTION reject_published_snapshot_set_change()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status = 'PUBLISHED' THEN
            RAISE EXCEPTION 'immutable_snapshot: snapshot_set % is published', OLD.id
                USING ERRCODE = '55000';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_set_immutable
    BEFORE UPDATE OR DELETE ON snapshot_set
    FOR EACH ROW EXECUTE FUNCTION reject_published_snapshot_set_change()
    """,
    """
    CREATE FUNCTION validate_snapshot_set_member()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM snapshot_set AS snapshot_group
            JOIN accounting_snapshot AS snapshot
              ON snapshot.id = NEW.snapshot_id
            WHERE snapshot_group.id = NEW.snapshot_set_id
              AND snapshot.company_id = NEW.company_id
              AND snapshot.period = snapshot_group.period
              AND snapshot.status = 'PUBLISHED'
        ) THEN
            RAISE EXCEPTION 'snapshot_set_member must reference a published same-company period snapshot'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_set_member_validate
    BEFORE INSERT OR UPDATE ON snapshot_set_member
    FOR EACH ROW EXECUTE FUNCTION validate_snapshot_set_member()
    """,
    """
    CREATE FUNCTION reject_published_snapshot_set_member_change()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        row_id UUID;
        old_parent_id UUID;
        new_parent_id UUID;
        parent_row RECORD;
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            row_id := OLD.id;
            old_parent_id := OLD.snapshot_set_id;
        END IF;

        IF TG_OP <> 'DELETE' THEN
            row_id := NEW.id;
            new_parent_id := NEW.snapshot_set_id;
        END IF;

        FOR parent_row IN
            SELECT id, status
            FROM snapshot_set
            WHERE id = old_parent_id OR id = new_parent_id
            ORDER BY id
            FOR UPDATE
        LOOP
            IF parent_row.status = 'PUBLISHED' THEN
                RAISE EXCEPTION
                    'immutable_snapshot: snapshot_set_member % belongs to published set %',
                    row_id, parent_row.id USING ERRCODE = '55000';
            END IF;
        END LOOP;

        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_snapshot_set_member_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON snapshot_set_member
    FOR EACH ROW EXECUTE FUNCTION reject_published_snapshot_set_member_change()
    """,
)


def upgrade() -> None:
    for enum_name, values in ENUMS:
        quoted_values = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {enum_name} AS ENUM ({quoted_values})")

    for statement in TABLE_DDL:
        op.execute(statement)

    for statement in INDEX_DDL:
        op.execute(statement)

    for statement in TRIGGER_DDL:
        op.execute(statement)


def downgrade() -> None:
    for function_name in (
        "reject_published_snapshot_set_member_change",
        "validate_snapshot_set_member",
        "reject_published_snapshot_set_change",
        "validate_snapshot_set_completeness",
        "assign_snapshot_set_published_at",
        "reject_published_snapshot_source_change",
        "reject_published_accounting_snapshot_change",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function_name}() CASCADE")

    for table_name in (
        "audit_event",
        "review_action",
        "risk_case",
        "detection_record",
        "monitoring_run",
        "snapshot_set_member",
        "snapshot_set",
        "snapshot_source",
        "accounting_snapshot",
        "rule_version",
        "tax_master_version",
        "source_record",
        "ingest_error",
        "ingest_batch",
        "company",
    ):
        op.execute(f"DROP TABLE {table_name}")

    for enum_name, _ in reversed(ENUMS):
        op.execute(f"DROP TYPE {enum_name}")

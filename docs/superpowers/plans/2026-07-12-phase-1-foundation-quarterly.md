# Phase 1 Foundation and Quarterly Monitoring Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-shaped Phase 1 foundation that ingests controlled quarterly data for 100+ companies, publishes immutable quality-gated snapshots, executes the three approved deterministic tax calculations, creates auditable risk cases, and exposes a minimal quarterly dashboard.

**Architecture:** Use a modular Python service with pure domain calculations, SQLAlchemy repositories, FastAPI APIs, and Celery company-partitioned batch workers. PostgreSQL is the source of truth for control-plane, lineage, calculations, and cases; Redis carries durable task coordination; React consumes read-only quarterly APIs. No LLM, embedding, prompt, vector store, or semantic Agent code belongs in Phase 1.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2/Alembic, PostgreSQL 16, Celery 5/Redis 7, pytest/Hypothesis; React 18, TypeScript, Vite, Ant Design, TanStack Query, Vitest/Testing Library; Docker Compose for local infrastructure.

---

**Source specification:** docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md

**Execution rules:** Follow @superpowers:test-driven-development for every behavior, @superpowers:verification-before-completion before each chunk handoff, and commit only after the named focused checks pass. Commands assume the repository root unless a command begins with cd.

## Planned File Map

### Backend foundation

- **backend/pyproject.toml** — Python package, runtime and test dependencies, pytest/coverage configuration.
- **backend/src/tax_risk/config.py** — environment-backed settings.
- **backend/src/tax_risk/db.py** — SQLAlchemy engine, session factory, declarative base.
- **backend/src/tax_risk/main.py** — FastAPI application factory.
- **backend/src/tax_risk/domain/money.py** — Money, Rate, ROUND_HALF_UP contract.
- **backend/src/tax_risk/domain/quarterly.py** — pure quarterly inputs, results, and formulas.
- **backend/src/tax_risk/domain/cases.py** — risk fingerprint and state transition policy.
- **backend/src/tax_risk/persistence/models.py** — canonical SQLAlchemy Base and focused-model import registry; later phases keep this canonical path.
- **backend/src/tax_risk/persistence/repositories.py** — canonical transaction-scoped UnitOfWork and focused-repository composition; later phases keep this canonical path.
- **backend/src/tax_risk/persistence/ingest_models.py** — Company, IngestBatch, IngestError, and SourceRecord tables.
- **backend/src/tax_risk/persistence/master_models.py** — effective-dated tax-master and rule-version tables.
- **backend/src/tax_risk/persistence/snapshot_models.py** — AccountingSnapshot, SnapshotSource, SnapshotSet, and SnapshotSetMember tables.
- **backend/src/tax_risk/persistence/risk_models.py** — MonitoringRun, DetectionRecord, RiskCase, ReviewAction, and AuditEvent tables.
- **backend/src/tax_risk/persistence/ingest_repositories.py** — company and ingestion persistence operations.
- **backend/src/tax_risk/persistence/master_repositories.py** — tax-master and rule-version operations.
- **backend/src/tax_risk/persistence/snapshot_repositories.py** — quality-gate, locking, and snapshot publication operations.
- **backend/src/tax_risk/persistence/risk_repositories.py** — run, detection, case, review, and audit operations.
- **backend/src/tax_risk/application/ingest.py** — IngestBatch use cases.
- **backend/src/tax_risk/application/companies.py** — SAP company reference import and active-company lookup.
- **backend/src/tax_risk/application/master_data.py** — versioned tax master import and lookup.
- **backend/src/tax_risk/application/snapshots.py** — quality gate and immutable snapshot publication.
- **backend/src/tax_risk/application/quarterly_runs.py** — formula execution, detections, and cases.
- **backend/src/tax_risk/adapters/ingest/base.py** — canonical batch adapter protocol.
- **backend/src/tax_risk/adapters/ingest/csv_adapter.py** — reference CSV adapter.
- **backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py** — controlled XLSX master adapter.
- **backend/src/tax_risk/api/schemas.py** — Pydantic v2 transport schemas.
- **backend/src/tax_risk/api/dependencies.py** — sessions and principal scope.
- **backend/src/tax_risk/api/routes/** — health, ingest, master data, snapshots, runs, cases, dashboard.
- **backend/src/tax_risk/workers/celery_app.py** — Celery configuration and routing.
- **backend/src/tax_risk/workers/quarterly_batch.py** — fan-out, company task, fan-in summary.
- **backend/migrations/** — Alembic environment and numbered schema migrations.
- **backend/tests/unit/** — pure domain and application tests.
- **backend/tests/integration/** — PostgreSQL/API/Celery eager-mode tests.
- **backend/tests/e2e/** — standard-data full quarterly acceptance.

### Frontend and infrastructure

- **web/src/api/client.ts** — typed HTTP client.
- **web/src/api/quarterly.ts** — dashboard/run/case queries.
- **web/src/features/quarterly/types.ts** — UI contracts.
- **web/src/features/quarterly/QuarterlyDashboardPage.tsx** — minimum dashboard.
- **web/src/features/quarterly/QuarterlyRunTable.tsx** — company status and risk table.
- **web/src/features/quarterly/FormulaDrawer.tsx** — calculation substitution and lineage.
- **web/src/App.tsx** — router and query provider.
- **web/src/**/*.test.tsx** — component tests.
- **web/e2e/quarterly-dashboard.spec.ts** — browser acceptance.
- **infra/docker-compose.yml** — PostgreSQL, Redis, API, worker, and web.
- **infra/env.example** — non-secret local configuration.
- **infra/README.md** — start, migrate, seed, run, and verify commands.

## Chunk 1: Foundation, Data Contracts, and Immutable Snapshot

### Task 1: Bootstrap the greenfield backend and frontend

**Files:**
- Create: **backend/pyproject.toml**
- Create: **backend/src/tax_risk/__init__.py**
- Create: **backend/src/tax_risk/config.py**
- Create: **backend/src/tax_risk/db.py**
- Create: **backend/src/tax_risk/main.py**
- Create: **backend/src/tax_risk/api/routes/health.py**
- Create: **backend/tests/unit/api/test_health.py**
- Create: **web/package.json**
- Create: **web/tsconfig.json**
- Create: **web/vite.config.ts**
- Create: **web/index.html**
- Create: **web/src/main.tsx**
- Create: **web/src/App.tsx**
- Create: **web/src/App.test.tsx**
- Create: **infra/docker-compose.yml**
- Create: **infra/env.example**
- Create: **.gitignore**

- [ ] **Step 1: Add package manifests and the failing health tests**

Use Python 3.12 and declare FastAPI, Pydantic Settings, SQLAlchemy, Alembic, psycopg, Celery, Redis, python-multipart, openpyxl, pytest, pytest-cov, Hypothesis, httpx, Ruff, and mypy in **backend/pyproject.toml**. Configure pytest with pythonpath=src and strict markers. Add this backend test:

~~~python
from fastapi.testclient import TestClient

from tax_risk.main import create_app


def test_health_reports_service_ready() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tax-risk"}
~~~

Configure React/Vite/TypeScript, Ant Design, @tanstack/react-query, Vitest, Testing Library, Playwright, ESLint, and Prettier. Add an App test expecting the heading “集团所得税风险监测”.

- [ ] **Step 2: Run tests and verify the red state**

Run: cd backend && python3.12 -m pip install -e '.[dev]' && pytest tests/unit/api/test_health.py -q

Expected: FAIL because tax_risk.main or create_app does not exist.

Run: cd web && npm install && npm test -- --run

Expected: FAIL because App does not yet render the required heading.

- [ ] **Step 3: Implement the smallest application shells**

Create a Settings class with database_url, redis_url, environment, and development_principal_enabled. Implement create_app as an application factory, include a GET /health router, and expose no business endpoints yet. Build App with Ant Design Layout and the required heading. Configure **infra/docker-compose.yml** with PostgreSQL 16 and Redis 7 health checks; do not add semantic-model services.

- [ ] **Step 4: Verify the green state and static checks**

Run: cd backend && pytest tests/unit/api/test_health.py -q && ruff check src tests && mypy src

Expected: 1 passed; Ruff and mypy exit 0.

Run: cd web && npm test -- --run && npm run build

Expected: App test passes and Vite build exits 0.

- [ ] **Step 5: Commit the scaffold**

~~~bash
git add .gitignore backend web infra
git commit -m "chore: scaffold tax risk platform"
~~~

### Task 2: Implement Money, Rate, and rounding invariants

**Files:**
- Create: **backend/src/tax_risk/domain/money.py**
- Create: **backend/tests/unit/domain/test_money.py**
- Create: **backend/tests/unit/domain/test_rate_properties.py**

- [ ] **Step 1: Write example and property tests first**

Cover: string-only Decimal construction, same-currency arithmetic, currency/scale mismatch rejection, final ROUND_HALF_UP, no intermediate rounding, Rate range 0..1, 25%=0.25, and Hypothesis-generated values. The decisive examples are:

~~~python
from decimal import Decimal

import pytest

from tax_risk.domain.money import Money, Rate


def test_money_rounds_half_up_only_when_quantized() -> None:
    raw = Money.unrounded("1625000.005", currency="CNY", scale=2)

    assert raw.amount == Decimal("1625000.005")
    assert raw.quantized().amount == Decimal("1625000.01")


def test_money_rejects_binary_float() -> None:
    with pytest.raises(TypeError, match="Decimal-compatible string"):
        Money.unrounded(0.1, currency="CNY", scale=2)


def test_rate_stores_fraction_not_percent_number() -> None:
    assert Rate.from_fraction("0.25").value == Decimal("0.25")

    with pytest.raises(ValueError, match="between 0 and 1"):
        Rate.from_fraction("25")
~~~

- [ ] **Step 2: Prove the tests fail**

Run: cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py -q

Expected: collection FAIL because tax_risk.domain.money is missing.

- [ ] **Step 3: Implement the exact value-object contract**

Money must retain unrounded Decimal amount, ISO-like currency, and non-negative scale; quantized returns a new Money using Decimal.quantize with ROUND_HALF_UP. Addition/subtraction require equal currency and scale. Multiplication by Rate returns unrounded Money. Rate accepts Decimal/string only, normalizes to Decimal, and rejects values outside 0..1. Do not accept float anywhere.

- [ ] **Step 4: Verify examples and properties**

Run: cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py -q

Expected: all examples and at least 100 Hypothesis examples pass.

- [ ] **Step 5: Commit the numeric contract**

~~~bash
git add backend/src/tax_risk/domain/money.py backend/tests/unit/domain
git commit -m "feat: add exact money and rate values"
~~~

### Task 3: Create the control-plane database and first migration

**Files:**
- Create: **backend/src/tax_risk/persistence/__init__.py**
- Create: **backend/src/tax_risk/persistence/models.py**
- Create: **backend/src/tax_risk/persistence/repositories.py**
- Create: **backend/src/tax_risk/persistence/ingest_models.py**
- Create: **backend/src/tax_risk/persistence/master_models.py**
- Create: **backend/src/tax_risk/persistence/snapshot_models.py**
- Create: **backend/src/tax_risk/persistence/risk_models.py**
- Create: **backend/src/tax_risk/persistence/ingest_repositories.py**
- Create: **backend/src/tax_risk/persistence/master_repositories.py**
- Create: **backend/src/tax_risk/persistence/snapshot_repositories.py**
- Create: **backend/src/tax_risk/persistence/risk_repositories.py**
- Create: **backend/alembic.ini**
- Create: **backend/migrations/env.py**
- Create: **backend/migrations/script.py.mako**
- Create: **backend/migrations/versions/0001_control_plane.py**
- Create: **backend/tests/integration/persistence/test_schema.py**
- Create: **backend/tests/integration/persistence/test_constraints.py**

- [ ] **Step 1: Write schema and constraint tests**

Tests must assert tables for company, ingest_batch, ingest_error, tax_master_version, accounting_snapshot, snapshot_source, monitoring_run, detection_record, risk_case, review_action, and audit_event. Assert:

- unique SAP company_code and an auditable active/inactive lifecycle;
- unique ingest source+source_batch_key;
- unique source_record batch_id+source_record_key;
- unique tax master company+valid_from+version;
- unique immutable snapshot per company+period+source-version set and a SnapshotSet grouping the complete expected company-snapshot membership for one 100+ company run;
- SnapshotSet.published_at is a non-null UTC TIMESTAMPTZ only in PUBLISHED state and is written exactly once;
- published AccountingSnapshot, SnapshotSource, SnapshotSet, and SnapshotSetMember rows reject UPDATE/DELETE;
- unique risk_case fingerprint;
- Numeric(38,12) for calculation inputs/results and explicit currency/amount_scale;
- JSONB lineage and formula_substitution fields.

- [ ] **Step 2: Run against PostgreSQL and verify failure**

Run: docker compose -f infra/docker-compose.yml up -d postgres && cd backend && alembic upgrade head && pytest tests/integration/persistence -q

Expected: Alembic or tests FAIL because migration 0001 and models are absent.

- [ ] **Step 3: Implement models, repositories, and migration**

Use UUID primary keys, UTC timestamptz audit fields, PostgreSQL enums for states, foreign keys, and check constraints. Keep the canonical Base in `persistence/models.py` and the canonical UnitOfWork/session boundary in `persistence/repositories.py`; focused model and repository files must not create another Base, engine, session factory, or UnitOfWork. Include SourceRecord, SnapshotSet, RuleVersion, and SnapshotSetMember in addition to the named tables. Store source money in Numeric(38,12); store Rate values in Numeric(20,12). Protect published AccountingSnapshot/SnapshotSource and PUBLISHED SnapshotSet/SnapshotSetMember rows with PostgreSQL triggers in migration 0001 that raise immutable_snapshot on UPDATE or DELETE.

- [ ] **Step 4: Recreate and verify the database**

Run: docker compose -f infra/docker-compose.yml exec -T postgres dropdb -U tax_risk --if-exists tax_risk && docker compose -f infra/docker-compose.yml exec -T postgres createdb -U tax_risk tax_risk && cd backend && alembic upgrade head && pytest tests/integration/persistence -q

Expected: migration reaches head; all schema/constraint tests pass, including rejected UPDATE of a published snapshot.

- [ ] **Step 5: Commit the persisted control plane**

~~~bash
git add backend/src/tax_risk/persistence backend/alembic.ini backend/migrations backend/tests/integration/persistence
git commit -m "feat: add auditable control-plane schema"
~~~

### Task 4: Add IngestBatch API and the reference bulk-file adapter

**Files:**
- Create: **backend/src/tax_risk/adapters/ingest/base.py**
- Create: **backend/src/tax_risk/adapters/ingest/csv_adapter.py**
- Create: **backend/src/tax_risk/application/ingest.py**
- Create: **backend/src/tax_risk/api/schemas.py**
- Create: **backend/src/tax_risk/api/routes/ingest.py**
- Modify: **backend/src/tax_risk/main.py**
- Create: **backend/tests/fixtures/sap_quarterly_valid.csv**
- Create: **backend/tests/fixtures/sap_quarterly_invalid.csv**
- Create: **backend/tests/unit/adapters/test_csv_adapter.py**
- Create: **backend/tests/integration/api/test_ingest_batches.py**

- [ ] **Step 1: Write failing adapter and endpoint tests**

Define canonical rows with source_record_key, company_code, fiscal_year, period, currency, amount_scale, metric_code, amount, and extracted_at. Test POST /api/v1/ingest-batches, multipart POST /api/v1/ingest-batches/{batch_id}/files, and GET status. Required behavior:

- source+source_batch_key is idempotent;
- dataset_code=company_master creates or deactivates Company rows before financial datasets are accepted;
- SHA-256, row count, accepted count, rejected count, control total, and schema_version are stored;
- partial success returns row-numbered errors;
- unknown company and invalid Decimal are rejected, never coerced to zero;
- identical file retry returns the original batch rather than duplicating records.

- [ ] **Step 2: Verify tests fail**

Run: cd backend && pytest tests/unit/adapters/test_csv_adapter.py tests/integration/api/test_ingest_batches.py -q

Expected: FAIL because the ingest protocol, adapter, and routes are missing.

- [ ] **Step 3: Implement the canonical adapter and use case**

Define a BulkFileAdapter Protocol with validate_header and iter_rows. CSV adapter must use csv.DictReader and Decimal from strings, calculate SHA-256 while reading, and return structured row errors. The application service owns the transaction and state transitions RECEIVED→VALIDATING→SUCCEEDED/PARTIAL/FAILED and stores accepted canonical SourceRecord rows. CompanyService handles only dataset_code=company_master; financial records for unknown/inactive companies fail. The API accepts metadata before the file and never lets the adapter write the database directly.

- [ ] **Step 4: Verify idempotency and error reporting**

Run: cd backend && pytest tests/unit/adapters/test_csv_adapter.py tests/integration/api/test_ingest_batches.py -q

Expected: valid batch succeeds; invalid file is PARTIAL with exact rejected rows; replay leaves one batch.

- [ ] **Step 5: Commit ingestion**

~~~bash
git add backend/src/tax_risk/adapters backend/src/tax_risk/application/ingest.py backend/src/tax_risk/api backend/tests/fixtures backend/tests/unit/adapters backend/tests/integration/api/test_ingest_batches.py
git commit -m "feat: ingest controlled quarterly batches"
~~~

### Task 5: Import and resolve versioned tax master data

**Files:**
- Create: **backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py**
- Create: **backend/src/tax_risk/application/master_data.py**
- Create: **backend/src/tax_risk/api/routes/master_data.py**
- Modify: **backend/src/tax_risk/api/schemas.py**
- Modify: **backend/src/tax_risk/main.py**
- Create: **backend/tests/fixtures/tax_master_valid.xlsx**
- Create: **backend/tests/fixtures/tax_master_duplicate.xlsx**
- Create: **backend/tests/unit/adapters/test_tax_master_xlsx.py**
- Create: **backend/tests/integration/application/test_master_data.py**
- Create: **backend/tests/integration/api/test_tax_master_api.py**

- [ ] **Step 1: Write failing import and point-in-time lookup tests**

Required columns are company_code, company_name, valid_from, valid_to, tax_rate, loss_carryforward, three_year_average_tax_burden. Test 25% and 0.25 normalization to 0.25, rejection outside 0..1, non-negative loss, duplicate company/effective period rejection, overlapping approved versions rejection, file hash/audit metadata, maker-reviewer separation, and lookup by company+period.

- [ ] **Step 2: Verify the red state**

Run: cd backend && pytest tests/unit/adapters/test_tax_master_xlsx.py tests/integration/application/test_master_data.py tests/integration/api/test_tax_master_api.py -q

Expected: FAIL because tax master import and lookup do not exist.

- [ ] **Step 3: Implement staged import and approval**

Parse XLSX with openpyxl in read-only/data-only mode. Normalize percent-formatted cells and decimal strings through Rate; preserve source filename and SHA-256. Import as DRAFT, expose POST /api/v1/tax-master/import, POST /api/v1/tax-master/{version_id}/approve, and GET /api/v1/tax-master/{company_code}?period=YYYY-QN. Approval must reject the uploader as reviewer and reject overlaps in one transaction.

- [ ] **Step 4: Verify master-data safety**

Run: cd backend && pytest tests/unit/adapters/test_tax_master_xlsx.py tests/integration/application/test_master_data.py tests/integration/api/test_tax_master_api.py -q

Expected: valid import and separate approval pass; duplicates, overlap, missing company, and same-person approval fail with stable error codes.

- [ ] **Step 5: Commit tax master data**

~~~bash
git add backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py backend/src/tax_risk/application/master_data.py backend/src/tax_risk/api backend/tests/fixtures/tax_master_*.xlsx backend/tests/unit/adapters/test_tax_master_xlsx.py backend/tests/integration
git commit -m "feat: govern versioned tax master data"
~~~

### Task 6: Publish immutable snapshots through a quality gate

**Files:**
- Create: **backend/src/tax_risk/application/snapshots.py**
- Create: **backend/src/tax_risk/api/routes/snapshots.py**
- Modify: **backend/src/tax_risk/api/schemas.py**
- Modify: **backend/src/tax_risk/main.py**
- Create: **backend/tests/unit/application/test_snapshot_quality.py**
- Create: **backend/tests/integration/application/test_snapshot_publication.py**
- Create: **backend/tests/integration/api/test_snapshots_api.py**

- [ ] **Step 1: Write failing quality-gate tests**

Define required quarterly metric codes: cumulative_profit, received_dividends, fair_value_change, cumulative_revenue, prior_quarter_current_tax, current_quarter_current_tax, other_payables_accrual, hesi_no_invoice. Test:

- all required source batches succeeded or explicitly accepted partials;
- company/period/currency/amount_scale agree;
- one approved tax master resolves;
- duplicate metric and control-total mismatch block publication;
- missing master or source creates a DATA_QUALITY detection, not a zero value;
- snapshot hash is stable for identical ordered source hashes;
- one SnapshotSet can contain exactly one published company snapshot per requested company and period;
- SnapshotSet publication fails without writing a set, member, or published_at when any expected member is missing, invalid, unlocked, mixed-period, duplicated, or fails its quality gate;
- all expected members are locked, inserted, and atomically transitioned with the SnapshotSet to PUBLISHED in one transaction;
- published_at is generated by the database in UTC exactly once, is returned by the API as a timezone-aware RFC 3339 UTC value, and is the sole Phase 4 data_ready_at;
- published AccountingSnapshot/SnapshotSource and PUBLISHED SnapshotSet/SnapshotSetMember rows reject UPDATE/DELETE.

- [ ] **Step 2: Prove quality checks are absent**

Run: cd backend && pytest tests/unit/application/test_snapshot_quality.py tests/integration/application/test_snapshot_publication.py tests/integration/api/test_snapshots_api.py -q

Expected: FAIL because snapshot quality and publication services are missing.

- [ ] **Step 3: Implement validate-then-publish**

Create POST /api/v1/snapshots/validate, POST /api/v1/snapshots/{id}/publish, and POST /api/v1/snapshot-sets. Validation returns error_code, source, field, company, period, and remediation. AccountingSnapshot publication locks source batch IDs and approved tax master ID, computes a deterministic SHA-256 over canonical metadata, and transitions DRAFT→VALIDATED→PUBLISHED in one transaction.

SnapshotSet publication receives the complete expected member list, locks every referenced published AccountingSnapshot and source/master membership, reruns the set-level quality gate, inserts all SnapshotSetMember rows, transitions the set to PUBLISHED, and writes database UTC `published_at` exactly once in the same transaction. Any failed member or concurrent change rolls the whole transaction back, leaving no SnapshotSet, member, or timestamp. The response schema returns `published_at` as a timezone-aware RFC 3339 UTC value. A PUBLISHED set and its members are immutable; corrected membership creates a new set with `supersedes_snapshot_set_id`. Downstream phases must use SnapshotSet.published_at—and no upload, model-call, or batch-start timestamp—as `data_ready_at`.

- [ ] **Step 4: Run snapshot tests and Chunk 1 regression**

Run: cd backend && pytest tests/unit tests/integration -q && ruff check src tests && mypy src

Expected: all Chunk 1 tests pass; failed set publication leaves no rows/timestamp; successful publication writes one UTC published_at and immutable complete membership; no rule or Agent tests exist yet.

- [ ] **Step 5: Commit and mark the Chunk 1 checkpoint**

~~~bash
git add backend/src/tax_risk/application/snapshots.py backend/src/tax_risk/api backend/tests
git commit -m "feat: publish quality-gated immutable snapshots"
~~~

Before starting Chunk 2, run a plan/document checkpoint against the source specification and confirm that Chunk 1 contains no semantic Agent, vector, prompt, or model dependency.

## Chunk 2: Quarterly Rules, Cases, Parallel Execution, and Product Slice

### Task 7: Implement the three deterministic quarterly calculations

**Files:**
- Create: **backend/src/tax_risk/domain/quarterly.py**
- Create: **backend/tests/unit/domain/test_quarterly_examples.py**
- Create: **backend/tests/unit/domain/test_quarterly_properties.py**
- Create: **backend/tests/unit/domain/test_quarterly_errors.py**

- [ ] **Step 1: Write the approved examples and edge cases first**

Tests must assert the standard example:

~~~python
from decimal import Decimal

from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import QuarterlyInputs, calculate_quarterly


def test_standard_quarterly_example() -> None:
    result = calculate_quarterly(
        QuarterlyInputs(
            cumulative_profit=Money.unrounded("10000000", "CNY", 2),
            received_dividends=Money.unrounded("1000000", "CNY", 2),
            fair_value_change=Money.unrounded("500000", "CNY", 2),
            loss_carryforward=Money.unrounded("2000000", "CNY", 2),
            tax_rate=Rate.from_fraction("0.25"),
            prior_quarter_current_tax=Money.unrounded("900000", "CNY", 2),
            current_quarter_current_tax=Money.unrounded("700000", "CNY", 2),
            cumulative_revenue=Money.unrounded("50000000", "CNY", 2),
            historical_average_tax_burden=Rate.from_fraction("0.09"),
            other_payables_accrual=Money.unrounded("1400000", "CNY", 2),
            hesi_no_invoice=Money.unrounded("300000", "CNY", 2),
        )
    )

    assert result.cumulative_tax_payable.amount == Decimal("1625000.00")
    assert result.current_quarter_should_accrue.amount == Decimal("725000.00")
    assert result.current_quarter_difference.amount == Decimal("25000.00")
    assert result.accrual_alert_code == "UNDER_ACCRUED"
    assert result.current_tax_burden == Decimal("0.0325")
    assert result.tax_burden_deviation == Decimal("-0.0575")
    assert result.tax_burden_alert_code == "TAX_BURDEN_LOW"
    assert result.potential_adjustment.amount == Decimal("1700000.00")
    assert result.potential_tax_payable.amount == Decimal("2050000.00")
    assert result.potential_tax_cost.amount == Decimal("425000.00")
    assert result.potential_tax_cost_alert_code == "POTENTIAL_TAX_COST"
~~~

Also test zero/negative profit, negative fair-value change, full/partial loss offset, red entries, negative current-quarter required accrual, revenue≤0 NOT_CALCULABLE, and final-only rounding. `received_dividends` is the SAP ledger's current-year cumulative **received** dividend amount, excludes dividends paid/distributed by the company, and retains SAP reversal signs. `historical_average_tax_burden` is matched from the approved company master version and is never recalculated by the platform.

Lock these pre-floor boundaries:

- `base_before_floor=-100` and `potential_adjustment=60` produce `cumulative_base=0`, `potential_base=0`, zero potential tax cost, and no potential-cost alert.
- `base_before_floor=-100` and `potential_adjustment=150` produce `cumulative_base=0` and `potential_base=50`; at rate 25% the potential tax/cost is 12.50 and triggers `POTENTIAL_TAX_COST`.

Lock all alert boundaries:

- current-quarter difference >0 → `UNDER_ACCRUED`; <0 → `OVER_ACCRUED`; =0 → no accrual alert;
- tax-burden deviation >=+0.05 → `TAX_BURDEN_HIGH`; <=-0.05 → `TAX_BURDEN_LOW`; -0.05<deviation<+0.05 → no burden alert;
- potential tax cost !=0 → `POTENTIAL_TAX_COST`; =0 → no potential-cost alert.

Add a rounding-sensitive case proving tax burden divides the already ROUND_HALF_UP cumulative tax payable by cumulative revenue, not the unrounded tax product. Hypothesis properties: cumulative tax is non-negative; increasing a non-negative potential adjustment cannot decrease potential tax; identical inputs are deterministic.

- [ ] **Step 2: Run and observe failure**

Run: cd backend && pytest tests/unit/domain/test_quarterly_examples.py tests/unit/domain/test_quarterly_properties.py tests/unit/domain/test_quarterly_errors.py -q

Expected: FAIL because quarterly domain types and calculate_quarterly are missing.

- [ ] **Step 3: Implement pure formulas exactly once**

Implement immutable QuarterlyInputs and QuarterlyResult. Use these formulas with unrounded Decimal intermediates:

~~~text
base_before_floor = profit - received_dividends - fair_value_change - loss_carryforward
cumulative_base = max(base_before_floor, 0)
cumulative_tax = round_half_up(cumulative_base × tax_rate)
current_should_accrue = round_half_up(cumulative_tax - prior_quarter_current_tax)
current_difference = round_half_up(current_should_accrue - current_quarter_current_tax)
tax_burden = cumulative_tax / cumulative_revenue
deviation = tax_burden - three_year_average_tax_burden
potential_adjustment = other_payables_accrual + hesi_no_invoice
potential_base = max(base_before_floor + potential_adjustment, 0)
potential_tax = round_half_up(potential_base × tax_rate)
potential_tax_cost = round_half_up(potential_tax - cumulative_tax)
~~~

Do not derive `potential_base` from the already floored `cumulative_base`. `cumulative_tax` and `potential_tax` are each rounded once with ROUND_HALF_UP at the company ledger scale; tax burden uses the rounded `cumulative_tax` numerator and an unrounded Decimal division result. Compare that display-independent deviation with Decimal("0.05"). Do not apply max to `current_should_accrue`.

Output currency, amount_scale, CALCULATED/NOT_CALCULABLE/FAILED, alert flags/codes, nullable values, not_calculated_reason, and a formula_substitution mapping containing both `base_before_floor` and `cumulative_base`. The calculator receives the approved master-provided three-year average burden; it has no code path for calculating historical averages.

- [ ] **Step 4: Verify all numerical contracts**

Run: cd backend && pytest tests/unit/domain/test_quarterly_*.py -q

Expected: standard values and `POTENTIAL_TAX_COST` match exactly; the -100+60 and -100+150 pre-floor cases pass; accrual positive/negative/zero, burden exact ±5 percentage points/interior, and potential-cost nonzero/zero alert boundaries pass; revenue≤0 has null burden values and a reason code; all properties pass.

- [ ] **Step 5: Commit formulas**

~~~bash
git add backend/src/tax_risk/domain/quarterly.py backend/tests/unit/domain/test_quarterly_*.py
git commit -m "feat: calculate quarterly tax risks deterministically"
~~~

### Task 8: Persist detections and enforce risk fingerprints/state machine

**Files:**
- Create: **backend/src/tax_risk/domain/cases.py**
- Create: **backend/src/tax_risk/application/quarterly_runs.py**
- Create: **backend/tests/unit/domain/test_case_fingerprint.py**
- Create: **backend/tests/unit/domain/test_case_state_machine.py**
- Create: **backend/tests/integration/application/test_quarterly_run.py**

- [ ] **Step 1: Write failing case tests**

Test numeric fingerprint as SHA-256 of company_code|fiscal_year|quarter|monitoring_type, excluding rule/model version. Different quarters/types must not collide. Define allowed states:

NEW→ASSIGNED→PENDING_COMPANY_CONFIRMATION;
confirmed branch to PENDING_ADJUSTMENT→ADJUSTED_PENDING_REVIEW→CLOSED;
reasonable branch to GROUP_REVIEW→CLOSED;
information branch to EVIDENCE_REQUIRED→PENDING_COMPANY_CONFIRMATION.

Test illegal transitions fail and every detection is retained. Case creation is isolated by monitoring type:

- ACCRUAL_ACCURACY creates a case only for `UNDER_ACCRUED` or `OVER_ACCRUED`, never for zero difference;
- TAX_BURDEN creates a case only for `TAX_BURDEN_HIGH` or `TAX_BURDEN_LOW`, including exact ±0.05, never for an interior or uncalculable deviation;
- POTENTIAL_TAX_COST creates a case only when cost is nonzero and the code is `POTENTIAL_TAX_COST`.

For one company/quarter, an alert or zero in one monitor must not create, suppress, close, or overwrite another monitor's case. A fixture with all three alerts creates three distinct fingerprints/cases; mixed fixtures create exactly the alerting subset. Rerun adds detections without duplicate cases, and DATA_QUALITY/NOT_CALCULABLE never appears as “no risk”.

- [ ] **Step 2: Verify the red state**

Run: cd backend && pytest tests/unit/domain/test_case_*.py tests/integration/application/test_quarterly_run.py -q

Expected: FAIL because fingerprints, transitions, and quarterly run service are absent.

- [ ] **Step 3: Implement cases and run transaction**

QuarterlyRunService loads one PUBLISHED snapshot and one approved RuleVersion with code QUARTERLY_V1, materializes QuarterlyInputs, executes the pure calculator, writes one DetectionRecord per monitoring type, and upserts RiskCase by fingerprint. Persist formula substitution, snapshot/rule/master version, source lineage, currency, scale, calculation_status, alert code, and direction. Store the reviewed QUARTERLY_V1 formula manifest and SHA-256 through migration/seed code rather than accepting free-form formulas over the API. Use a transaction and PostgreSQL unique constraint to make retries safe.

- [ ] **Step 4: Verify persistence and retry behavior**

Run: cd backend && pytest tests/unit/domain/test_case_*.py tests/integration/application/test_quarterly_run.py -q

Expected: legal transitions pass; illegal transition fails; alert direction/zero/±0.05 boundaries create exactly the expected isolated case subset; running twice yields two detections and one case per alerting monitoring type.

- [ ] **Step 5: Commit case lifecycle**

~~~bash
git add backend/src/tax_risk/domain/cases.py backend/src/tax_risk/application/quarterly_runs.py backend/tests/unit/domain/test_case_*.py backend/tests/integration/application/test_quarterly_run.py
git commit -m "feat: create idempotent quarterly risk cases"
~~~

### Task 9: Orchestrate a 100+ company quarterly batch with Celery

**Files:**
- Create: **backend/src/tax_risk/workers/celery_app.py**
- Create: **backend/src/tax_risk/workers/quarterly_batch.py**
- Create: **backend/tests/unit/workers/test_quarterly_batch_canvas.py**
- Create: **backend/tests/integration/workers/test_quarterly_batch_eager.py**
- Create: **backend/tests/integration/workers/test_quarterly_batch_105_companies.py**
- Modify: **infra/docker-compose.yml**

- [ ] **Step 1: Write fan-out, idempotency, and isolation tests**

Create 105 companies: 103 valid, one missing master, one malformed source batch. Assert the orchestrator builds one company task per SnapshotSet member, routes by run_type=quarterly, caps configurable concurrency, uses run key fiscal_year+quarter+snapshot_set_id+rule_version, records per-company success/blocked/failed, allows failed-company-only retry, and final summary is 103 succeeded and 2 blocked/failed without rolling back successes.

- [ ] **Step 2: Verify Celery tests fail**

Run: cd backend && pytest tests/unit/workers/test_quarterly_batch_canvas.py tests/integration/workers/test_quarterly_batch_eager.py tests/integration/workers/test_quarterly_batch_105_companies.py -q

Expected: FAIL because Celery application and tasks are missing.

- [ ] **Step 3: Implement group/chord orchestration**

Configure JSON-only serialization, UTC timestamps, acknowledgements after work, reject-on-worker-lost, task time limits, retry_backoff, retry_jitter, and separate quarterly queue. Use a Celery group of run_company_quarterly tasks followed by summarize_quarterly_batch. Company tasks call QuarterlyRunService and return only IDs/status, never full financial rows. Enforce database idempotency in addition to Celery task IDs.

- [ ] **Step 4: Verify 105-company behavior**

Run: cd backend && CELERY_TASK_ALWAYS_EAGER=true pytest tests/unit/workers tests/integration/workers -q

Expected: all tests pass; 103 successful company results remain committed; retrying two failures creates no duplicate cases.

- [ ] **Step 5: Commit orchestration**

~~~bash
git add backend/src/tax_risk/workers backend/tests/unit/workers backend/tests/integration/workers infra/docker-compose.yml
git commit -m "feat: run quarterly monitoring across companies"
~~~

### Task 10: Expose minimum secured quarterly APIs

**Files:**
- Create: **backend/src/tax_risk/security/principal.py**
- Create: **backend/src/tax_risk/api/dependencies.py**
- Create: **backend/src/tax_risk/api/routes/runs.py**
- Create: **backend/src/tax_risk/api/routes/cases.py**
- Create: **backend/src/tax_risk/api/routes/dashboard.py**
- Modify: **backend/src/tax_risk/api/schemas.py**
- Modify: **backend/src/tax_risk/main.py**
- Create: **backend/tests/integration/api/test_quarterly_runs_api.py**
- Create: **backend/tests/integration/api/test_cases_scope.py**
- Create: **backend/tests/integration/api/test_dashboard_api.py**

- [ ] **Step 1: Write failing API and company-scope tests**

Required endpoints:

- POST /api/v1/quarterly-runs with fiscal_year, quarter, snapshot_set_id, rule_version;
- GET /api/v1/quarterly-runs/{run_id};
- GET /api/v1/risk-cases with year, quarter, monitoring_type, direction, status, company;
- POST /api/v1/risk-cases/{case_id}/actions;
- GET /api/v1/dashboard/quarterly?fiscal_year=&quarter=;
- GET /api/v1/detections/{detection_id} for formula substitution and lineage.

Test group-tax principal sees all; company-finance principal sees only its company; audit is read-only; unauthorized company ID is 404 rather than leaked 403; response Decimal values are strings and contain currency/scale/status/reason fields.

- [ ] **Step 2: Verify endpoint tests fail**

Run: cd backend && pytest tests/integration/api/test_quarterly_runs_api.py tests/integration/api/test_cases_scope.py tests/integration/api/test_dashboard_api.py -q

Expected: FAIL with 404 routes or missing principal dependency.

- [ ] **Step 3: Implement APIs and server-side scope**

Create Principal with subject, roles, allowed_company_ids, and organization_path. In development only, permit signed test headers behind development_principal_enabled; production must use an injected IdP verifier. Apply scope in repository SQL and keep PostgreSQL RLS migration-ready. Dashboard returns coverage_company_count, data_ready_count, blocked_count, risk_company_count, potential_tax_cost_total, monitoring-type counts, and paginated company rows.

- [ ] **Step 4: Verify API behavior and OpenAPI**

Run: cd backend && pytest tests/integration/api/test_* -q && python -c "from tax_risk.main import create_app; assert create_app().openapi()['paths']['/api/v1/dashboard/quarterly']"

Expected: all API tests pass; unauthorized data is absent; OpenAPI contains the quarterly dashboard path.

- [ ] **Step 5: Commit the API slice**

~~~bash
git add backend/src/tax_risk/security backend/src/tax_risk/api backend/tests/integration/api
git commit -m "feat: expose scoped quarterly risk APIs"
~~~

### Task 11: Build the minimum quarterly dashboard

**Files:**
- Create: **web/src/api/client.ts**
- Create: **web/src/api/quarterly.ts**
- Create: **web/src/features/quarterly/types.ts**
- Create: **web/src/features/quarterly/QuarterlyDashboardPage.tsx**
- Create: **web/src/features/quarterly/QuarterlyRunTable.tsx**
- Create: **web/src/features/quarterly/FormulaDrawer.tsx**
- Create: **web/src/features/quarterly/QuarterlyDashboardPage.test.tsx**
- Create: **web/src/features/quarterly/FormulaDrawer.test.tsx**
- Modify: **web/src/App.tsx**

- [ ] **Step 1: Write failing user-facing component tests**

Mock TanStack Query responses and assert:

- cards display coverage, ready, blocked, abnormal companies, and potential tax cost;
- selectors change year/quarter query keys;
- table separates data-quality blocked rows from risk rows;
- risk type, direction, actual/expected/difference values and status render;
- drawer shows formula, each substituted value, source, snapshot, master and rule versions;
- revenue≤0 displays “不可计算” and reason, never ¥0;
- no Agent or semantic-risk navigation appears.

- [ ] **Step 2: Verify the UI is red**

Run: cd web && npm test -- --run src/features/quarterly

Expected: FAIL because quarterly components and API functions are missing.

- [ ] **Step 3: Implement the typed dashboard**

Use Ant Design Statistic, Alert, Select, Table, Tag, Drawer, and Descriptions. TanStack Query owns server state; keep filters in URL search params; render money from string+currency+scale without JavaScript floating-point arithmetic. The details drawer consumes a detection endpoint rather than recomputing formulas in the browser.

- [ ] **Step 4: Verify components and production build**

Run: cd web && npm test -- --run && npm run lint && npm run build

Expected: all component tests pass; lint exits 0; Vite emits a production bundle.

- [ ] **Step 5: Commit the dashboard**

~~~bash
git add web/src
git commit -m "feat: add quarterly tax risk dashboard"
~~~

### Task 12: Complete full-stack E2E acceptance and operating instructions

**Files:**
- Create: **backend/tests/e2e/test_quarterly_standard_scenario.py**
- Create: **backend/tests/e2e/test_quarterly_api_worker_flow.py**
- Create: **backend/tests/e2e/seed_quarterly_scenario.py**
- Create: **web/e2e/quarterly-dashboard.spec.ts**
- Create: **web/playwright.config.ts**
- Modify: **infra/docker-compose.yml**
- Create: **infra/README.md**
- Create: **backend/README.md**
- Create: **web/README.md**

- [ ] **Step 1: Write failing end-to-end acceptance**

Seed 105 companies and include the approved standard company:

- profit 10,000,000; received dividends 1,000,000; fair-value gain 500,000; loss 2,000,000; rate 0.25;
- prior-quarter tax 900,000; current-quarter tax 700,000; revenue 50,000,000; historical burden 0.09;
- other-payables accrual 1,400,000; Hesi no-invoice 300,000.

API E2E must assert cumulative tax 1,625,000.00, should accrue 725,000.00, difference +25,000.00, current burden 0.0325, deviation -0.0575, potential adjustment 1,700,000.00, potential tax 2,050,000.00, potential cost 425,000.00, and potential alert code `POTENTIAL_TAX_COST`. Browser E2E must locate the company, open formula details, and show the same source values and versions.

- [ ] **Step 2: Run E2E and verify it fails before wiring**

Run: docker compose -f infra/docker-compose.yml up -d --build && cd backend && pytest tests/e2e/test_quarterly_standard_scenario.py -q

Expected: FAIL until seed, full API route wiring, and worker flow are complete.

Run: cd web && npx playwright test e2e/quarterly-dashboard.spec.ts

Expected: FAIL until the running dashboard can load seeded results.

- [ ] **Step 3: Configure the production-shaped Compose topology**

Configure `infra/docker-compose.yml` with postgres, redis, one-shot migrate, api, worker-quarterly, and web services. Give every long-running service a health check; use dependency health conditions rather than sleeps; mount no source directory into production-shaped containers; expose no semantic Agent/model service.

- [ ] **Step 4: Verify the Compose topology**

Run:

~~~bash
docker compose -f infra/docker-compose.yml config --quiet
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml ps
~~~

Expected: config exits 0; postgres, redis, api, worker-quarterly, and web are running/healthy; migrate exited successfully; no model service exists.

- [ ] **Step 5: Wire the API-worker-web acceptance path**

Use `seed_quarterly_scenario.py` to load the 105-company fixture through public phase1 ingestion/master/snapshot interfaces, atomically publish its SnapshotSet, submit a quarterly run through the API, let `worker-quarterly` process company tasks, poll the run endpoint to a terminal summary, and let the web dashboard read the persisted result. `test_quarterly_api_worker_flow.py` must use service URLs and API contracts rather than importing the application service directly.

- [ ] **Step 6: Verify the focused API-worker-web path**

Run:

~~~bash
cd backend
pytest tests/e2e/test_quarterly_api_worker_flow.py tests/e2e/test_quarterly_standard_scenario.py -q
cd ../web
npx playwright test e2e/quarterly-dashboard.spec.ts
~~~

Expected: both backend E2E tests and Playwright pass; the 103 valid companies remain committed, two blocked companies remain isolated, and the browser formula drawer shows the persisted API values.

- [ ] **Step 7: Write the infrastructure operations guide**

Write `infra/README.md` with exact, copyable commands for prerequisites, environment setup, start, health, migration, seed, quarterly submission, status polling, failed-company retry, log export, test backup/restore, and shutdown. State that SnapshotSet.published_at is the sole data-ready timestamp and that no model credential is required in Phase 1.

- [ ] **Step 8: Verify the infrastructure guide contract**

Run: `rg -n '^## (Prerequisites|Configure|Start|Health|Migrate|Seed|Submit|Inspect|Retry|Logs|Backup and Restore|Stop)$' infra/README.md`

Expected: all twelve required sections are present once and their commands reference `infra/docker-compose.yml`.

- [ ] **Step 9: Write the backend guide**

Write `backend/README.md` with Python 3.12 setup, database configuration, Alembic upgrade/downgrade-on-disposable-copy commands, API endpoints, Celery quarterly queue, company-only retry, unit/integration/E2E commands, Decimal/ROUND_HALF_UP rules, and the prohibition on semantic Agent dependencies.

- [ ] **Step 10: Verify the backend guide contract**

Run: `rg -n '^## (Setup|Database|Migrations|API|Quarterly Worker|Retry|Tests|Numeric Contract|Phase 1 Boundary)$' backend/README.md`

Expected: all required backend sections are present once.

- [ ] **Step 11: Write the web guide**

Write `web/README.md` with Node installation, `VITE_API_BASE_URL`, development start, Vitest, lint/typecheck, production build, Playwright E2E, and the quarterly dashboard route.

- [ ] **Step 12: Verify the web guide contract**

Run: `rg -n '^## (Setup|Environment|Development|Unit Tests|Lint and Typecheck|Build|Browser E2E|Quarterly Dashboard)$' web/README.md`

Expected: all required web sections are present once and no model/Agent configuration is documented.

- [ ] **Step 13: Run full verification and record expected evidence**

Run: cd backend && pytest --cov=tax_risk --cov-report=term-missing -q && ruff check src tests && mypy src

Expected: all backend tests pass; domain/quarterly.py and domain/money.py have 100% branch coverage; overall backend coverage is at least 90%; Ruff and mypy exit 0.

Run: cd web && npm test -- --run && npm run lint && npm run build && npx playwright test

Expected: all unit/component/browser tests pass; lint and build exit 0.

Run: docker compose -f infra/docker-compose.yml ps

Expected: postgres, redis, api, worker-quarterly, and web are healthy; migrate completed; no semantic Agent/model service is present.

- [ ] **Step 14: Commit the Phase 1 acceptance slice**

~~~bash
git add backend/tests/e2e backend/README.md web/e2e web/playwright.config.ts web/README.md infra
git commit -m "test: verify phase one quarterly monitoring"
~~~

At the end of Chunk 2, compare the implementation against Sections 2, 5, 6.1–6.3, 7, 9, 11, 12.1, 12.3, and 13.1 of the source specification. Record any accepted deviation in the relevant README and obtain business sign-off on field mapping, amount scale, and the 105-company acceptance report before production deployment.

# Phase 1 backend

The backend is a Python 3.12 FastAPI, SQLAlchemy/Alembic, PostgreSQL, and Celery application
for deterministic quarterly group income-tax monitoring.

## Setup

Create the development environment from the repository root:

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip setuptools
backend/.venv/bin/pip install --constraint backend/requirements.lock -e 'backend[dev]'
backend/.venv/bin/python -m pip check
```

The container build also applies `backend/requirements.lock` as a constraint. Update
`pyproject.toml` and the lock together whenever a runtime or development dependency changes.

## Database

Start the loopback-only local datastores and point host-side backend commands at PostgreSQL:

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d postgres redis
export DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export REDIS_URL='redis://127.0.0.1:6379/0'
```

Application containers use the Compose service names `postgres` and `redis`; host-side tests
use `127.0.0.1`. Replace all local-only credentials in any shared or deployed environment.

## Migrations

Inspect and upgrade the configured database:

```bash
cd backend
.venv/bin/alembic current
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

Never run `alembic downgrade` against the primary or only copy of a database. After restoring
a verified backup into a disposable database, a downgrade rehearsal may use that copy only:

```bash
cd backend
export DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk_restore_test'
.venv/bin/alembic downgrade -1
.venv/bin/alembic upgrade head
```

Discard the rehearsal database after verifying the migration path. Production downgrade is
permitted only after the same downgrade succeeds on a restored copy and the approved rollback
process authorizes it.

## API

Run the API directly for backend development:

```bash
cd backend
.venv/bin/uvicorn tax_risk.main:create_app --factory --host 127.0.0.1 --port 8000
```

Implemented endpoints are:

- `GET /health`
- `POST /api/v1/ingest-batches`, `POST /api/v1/ingest-batches/{id}/files`, and
  `GET /api/v1/ingest-batches/{id}`
- `POST /api/v1/tax-master/import`, `POST /api/v1/tax-master/{id}/approve`, and
  `GET /api/v1/tax-master/{company_code}`
- `POST /api/v1/snapshots/validate`, `POST /api/v1/snapshots/{id}/publish`, and
  `POST /api/v1/snapshot-sets`
- `POST /api/v1/quarterly-runs` and `GET /api/v1/quarterly-runs/{id}`
- `GET /api/v1/dashboard/quarterly`, `GET /api/v1/risk-cases`,
  `POST /api/v1/risk-cases/{id}/actions`, and `GET /api/v1/detections/{id}`

Every ingest-batch, tax-master, and snapshot control-plane route requires the `group-tax`
administrative role. Quarterly, risk, dashboard, and detection endpoints also enforce the
Principal's role and company scope in server-side SQL. The legacy `uploaded_by` and
`reviewed_by` transport fields are accepted for wire compatibility only; persisted maker and
reviewer identities come from `Principal.subject` and cannot be supplied by the request body.
Signed development headers are accepted only when both `ENVIRONMENT=development` and
`DEVELOPMENT_PRINCIPAL_ENABLED=true`. Production requires an injected IdP verifier and
otherwise returns HTTP 401; health remains public for orchestration.

## Quarterly Worker

Run the real quarterly queue locally:

```bash
cd backend
.venv/bin/celery -A tax_risk.workers.celery_app:celery_app worker --queues=quarterly --concurrency=4 --loglevel=INFO
```

The Compose equivalent is:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d worker-quarterly
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f worker-quarterly
```

Tasks carry durable IDs only. The worker reloads the frozen snapshot, tax-master version, and
rule version from PostgreSQL; it uses JSON serialization, late acknowledgement, worker-loss
rejection, bounded task timeouts, and per-company retry isolation.

## Retry

Automatic Celery retry applies only to a failed company attempt. Manual batch retry is the
`QuarterlyBatchService.retry_failed(run_id=...)` application operation: it is allowed only for
a terminal `PARTIAL_SUCCESS` or `FAILED` run and resets every and only `FAILED` company row.
It never recomputes `SUCCEEDED` companies and never retries data/control `BLOCKED` companies.

Phase 1 does not expose this operation as a public HTTP endpoint. Authorized operators must
use the controlled internal command in `infra/README.md`, record its returned company-task
IDs, and allow the normal Celery canvas to summarize the run again.

## Tests

Run the backend quality gates from the repository root:

```bash
backend/.venv/bin/pytest backend/tests -q
backend/.venv/bin/ruff check backend/src backend/tests infra/tests
backend/.venv/bin/mypy backend/src
```

Run the isolated deterministic E2E contracts against the loopback PostgreSQL service:

```bash
backend/.venv/bin/pytest -q backend/tests/e2e/test_quarterly_standard_scenario.py backend/tests/e2e/test_quarterly_eager_worker_contract.py
```

Run the deployed-service E2E only after the Compose API, Redis, and real quarterly worker are
healthy:

```bash
export E2E_BASE_URL=http://127.0.0.1:8000
export E2E_DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export E2E_DEV_PRINCIPAL_SECRET='local-only-tax-risk-development-secret-do-not-use-in-production'
export E2E_SEED_TOKEN="run$(date +%Y%m%d%H%M%S)"
export E2E_STANDARD_COMPANY_CODE="E2E-${E2E_SEED_TOKEN}-000"
export E2E_WORKER_TIMEOUT_SECONDS=300
backend/.venv/bin/pytest -q -s backend/tests/e2e/test_quarterly_api_worker_flow.py
```

This external test signs every control-plane request with separate group-tax maker and
reviewer subjects, seeds through HTTP, and exercises the real broker and worker. It is not
the in-process eager-worker contract.

## Numeric Contract

- Construct `Decimal` values from strings; never route accounting or tax amounts through
  binary floating point.
- Quantize monetary outputs with the governed currency and amount scale using
  `ROUND_HALF_UP`.
- Preserve full database precision for inputs and evidence. API Decimal values are exact
  strings accompanied by currency and scale where applicable.
- Rates are decimals, not percentages: `0.25` means 25%, and the alert threshold `0.05` means
  five percentage points.
- Formula replay uses the frozen snapshot, tax-master version, rule version, and persisted
  formula substitution. The browser does not recompute tax results.

## Phase 1 Boundary

Phase 1 implements controlled ingestion, immutable published snapshot sets, the three approved
quarterly deterministic checks, auditable risk cases, scoped APIs, and the quarterly dashboard.
`SnapshotSet.published_at` is the sole data-ready timestamp.

Phase 1 contains no semantic Agent, LLM adapter, model server, prompt, embedding index, or model
credential. Business-entertainment, welfare, and donation semantic classification are later
phases and must not be introduced into this runtime or worker queue.

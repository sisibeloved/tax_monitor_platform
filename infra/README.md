# Phase 1 infrastructure operations

This guide operates the production-shaped Phase 1 stack from the repository root. The
Compose topology contains PostgreSQL, Redis, a one-shot migration service, the API, the
quarterly worker, and the Web/Nginx service. It contains no semantic Agent or model service.

## Prerequisites

- Docker Engine with Docker Compose v2
- `curl` and `jq` for API operations
- Python 3.12 and the backend development environment for acceptance tests
- Node.js 22 for browser acceptance

Run all commands below from the repository root unless a command explicitly changes directory.

## Configure

Create the local acceptance environment and validate the expanded Compose configuration:

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml config --quiet
```

`infra/env.example` contains a fixed local-only development principal. Nginx injects its
signed headers on the server side; the HMAC secret is never included in the browser bundle.
Do not reuse those values outside an isolated developer machine.

For every deployed environment, replace the local database password and URLs through the
approved secret store, set `ENVIRONMENT=production`, disable and remove all
`DEVELOPMENT_PRINCIPAL_*` values, and inject the approved production IdP verifier into the
FastAPI application composition. Production fails closed with HTTP 401 when that verifier is
absent; enabling the development flag cannot bypass the production environment guard.

## Start

Build and start the complete stack:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
docker compose --env-file infra/.env -f infra/docker-compose.yml ps -a
```

`migrate` must finish with exit code 0. PostgreSQL, Redis, API, `worker-quarterly`, and Web
must report healthy. PostgreSQL, Redis, API, and Web publish loopback-only host ports.

## Health

Check the service states and each health surface:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml ps -a
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T redis redis-cli ping
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T worker-quarterly celery -A tax_risk.workers.celery_app:celery_app inspect ping --timeout 5
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8080/healthz
```

The HTTP health endpoints are process health checks. Operational readiness also requires the
migration to have succeeded and PostgreSQL, Redis, and the quarterly worker checks above to
pass.

## Production Go/No-Go

This Phase 1 stack **MUST NOT be deployed to production** until every row below is completed
in the controlled release record and the final decision is `GO`. A local static test, a
successful image build, or an in-process eager-worker test does not satisfy these gates.

| Gate | Evidence that must be recorded | Status before evidence |
|---|---|---|
| Field mapping sign-off | Approved SAP/合思 field mapping, sign convention, source owner, reviewer, and approval date | `PENDING` |
| Amount scale sign-off | Currency and amount-scale mapping per company, `ROUND_HALF_UP` confirmation, reviewer, and approval date | `PENDING` |
| 105-company deployed-service E2E | `E2E_SEED_TOKEN`, monitoring `run_id`, execution timestamp, environment/image identifiers, and the exact result `105 requested / 103 succeeded / 2 blocked / 0 failed` | `PENDING` |
| Browser acceptance | `E2E_STANDARD_COMPANY_CODE`, Playwright result, trace or report location, execution timestamp, and confirmation that the formula drawer matched the persisted API values | `PENDING` |
| Business approval | Business approver, approval date, linked acceptance report, and explicit `GO` or `NO-GO` decision | `PENDING` |

The acceptance report must identify that the external E2E uses direct database access only
to resolve the fixed published rule and to inject the two documented post-publication drift
conditions. Those test-only operations are not production operating procedures. Any failed,
missing, stale, or unsigned row keeps the release at `NO-GO`.

## Migrate

Compose runs `alembic upgrade head` before starting the API and worker. To run the one-shot
migration explicitly and inspect its result:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml run --rm migrate
docker compose --env-file infra/.env -f infra/docker-compose.yml logs --no-color migrate
```

Never downgrade the primary database. Test any downgrade only on a disposable database made
from a verified backup, as described in the backend guide.

## Seed

There is no standalone deployment seed CLI. `backend/tests/e2e/seed_quarterly_scenario.py` is
a test helper, not a command. The external E2E test drives the public ingestion, tax-master,
snapshot, run, risk-list, and detection APIs against the running stack. It uses a direct
database connection only to locate the published rule and inject two deliberate
post-publication drift conditions.

Run a unique external 105-company seed and real API/Redis/Celery flow:

```bash
export E2E_BASE_URL=http://127.0.0.1:8000
export E2E_DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export E2E_DEV_PRINCIPAL_SECRET='local-only-tax-risk-development-secret-do-not-use-in-production'
export E2E_SEED_TOKEN="run$(date +%Y%m%d%H%M%S)"
export E2E_STANDARD_COMPANY_CODE="E2E-${E2E_SEED_TOKEN}-000"
export E2E_WORKER_TIMEOUT_SECONDS=300
backend/.venv/bin/pytest -q -s backend/tests/e2e/test_quarterly_api_worker_flow.py
```

The token must be unique, 6-32 characters, and contain only letters, digits, `_`, or `-`.
The database URL must point to the same database used by the API and worker. The test signs
all control-plane calls with distinct maker and reviewer subjects, uses the real broker and
worker, expects exactly 105 requested, 103 succeeded, two blocked, and zero failed companies,
and prints the standard company code needed by Playwright.

`SnapshotSet.published_at` is the sole authoritative data-ready timestamp. Upload time,
validation time, worker time, and dashboard-read time must never replace it.

## Submit

For a separately prepared published snapshot set, submit through the local Web proxy. The
local Nginx service adds the acceptance identity; production requests must instead be
authenticated by the injected IdP verifier.

```bash
export WEB_URL=http://127.0.0.1:8080
export SNAPSHOT_SET_ID='<published-snapshot-set-uuid>'
export RULE_VERSION_ID='<published-rule-version-uuid>'
RUN_RESPONSE=$(curl -fsS -X POST "$WEB_URL/api/v1/quarterly-runs" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg snapshot_set_id "$SNAPSHOT_SET_ID" \
    --arg rule_version "$RULE_VERSION_ID" \
    '{fiscal_year: 2026, quarter: 2, snapshot_set_id: $snapshot_set_id, rule_version: $rule_version}')")
printf '%s\n' "$RUN_RESPONSE" | jq .
export RUN_ID=$(printf '%s\n' "$RUN_RESPONSE" | jq -r .run_id)
```

Only an atomically published snapshot set and a published, approved quarterly rule version
are accepted.

## Inspect

Poll the persisted run, then inspect the dashboard and risk cases:

```bash
curl -fsS "$WEB_URL/api/v1/quarterly-runs/$RUN_ID" | jq .
curl -fsS "$WEB_URL/api/v1/dashboard/quarterly?fiscal_year=2026&quarter=2" | jq .
curl -fsS "$WEB_URL/api/v1/risk-cases?fiscal_year=2026&quarter=2&page=1&page_size=100" | jq .
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres psql -U tax_risk -d tax_risk -c "SELECT status, count(*) FROM monitoring_run_company WHERE run_id = '$RUN_ID' GROUP BY status ORDER BY status;"
```

Terminal run states are `SUCCEEDED`, `PARTIAL_SUCCESS`, or `FAILED`. `BLOCKED` is a
company-level data/control outcome and is not retried as a technical failure.

## Retry

Celery automatically retries retryable company tasks. Phase 1 has no public manual retry API.
If an authorized operator must requeue a terminal run, the following controlled internal
operation selects every and only company row in `FAILED`; it never re-runs `SUCCEEDED` or
`BLOCKED` companies:

```bash
export RUN_ID='<terminal-run-uuid>'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T api python - "$RUN_ID" <<'PY'
import sys
from uuid import UUID

from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.workers.celery_app import celery_app
from tax_risk.workers.quarterly_batch import build_quarterly_batch_canvas

plan = QuarterlyBatchService(UnitOfWork).retry_failed(run_id=UUID(sys.argv[1]))
if plan.run_company_ids:
    build_quarterly_batch_canvas(
        app=celery_app,
        run_id=plan.run_id,
        run_company_ids=plan.run_company_ids,
    ).apply_async()
print({
    "run_id": str(plan.run_id),
    "requeued_run_company_ids": [str(value) for value in plan.run_company_ids],
})
PY
```

Record the operator, change reference, run ID, and returned company-task IDs in the operating
log. An empty list means that the run had no `FAILED` companies eligible for this operation.

## Logs

Read live logs or export a support bundle without ANSI color:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api worker-quarterly web
mkdir -p artifacts/logs
docker compose --env-file infra/.env -f infra/docker-compose.yml logs --no-color --since=24h postgres redis migrate api worker-quarterly web > artifacts/logs/phase1-stack.log
```

Do not put uploaded source data, free-text evidence, database URLs, principal headers, or
secrets into tickets or shared log bundles.

## Backup and Restore

Create a custom-format backup and restore it only into a disposable verification database:

```bash
mkdir -p backups
export BACKUP_FILE="backups/tax_risk_$(date +%Y%m%d_%H%M%S).dump"
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP_FILE"
export TEST_DB=tax_risk_restore_test
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$TEST_DB" && createdb -U "$POSTGRES_USER" "$TEST_DB"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$TEST_DB" --exit-on-error' < "$BACKUP_FILE"
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'psql -U "$POSTGRES_USER" -d "$TEST_DB" -Atc "SELECT version_num FROM alembic_version"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'dropdb -U "$POSTGRES_USER" "$TEST_DB"'
```

Restoring over the primary database is intentionally not documented. Production restore and
rollback require the approved change process, an isolated target, checksum verification, and
a rehearsed application/migration rollback.

## Stop

Stop the stack while retaining PostgreSQL and Redis volumes:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml down
```

Delete volumes only for a disposable local environment after confirming no evidence or audit
history is needed:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml down --volumes
```

Phase 1 uses deterministic formulas and requires no model, LLM, or semantic Agent credential.

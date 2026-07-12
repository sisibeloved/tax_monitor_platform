# Phase 1 Web dashboard

The Web application is a React, TypeScript, Vite, Ant Design, and TanStack Query dashboard for
persisted quarterly tax-risk results.

## Setup

Install Node.js 22 and the exact locked dependencies from the repository root:

```bash
cd web
npm ci
```

Do not use `npm install` for acceptance or release builds because it may rewrite the lock file.

## Environment

`VITE_API_BASE_URL` is a build-time public URL prefix. Its default is empty, so browser calls
use the same origin and Compose Nginx proxies `/api/` to the API:

```bash
export VITE_API_BASE_URL=''
```

The local integrated path is `http://127.0.0.1:8080/`. Nginx injects the fixed local acceptance
principal only at runtime; `DEVELOPMENT_PRINCIPAL_SECRET` must never be placed in a `VITE_*`
variable, JavaScript source, browser storage, or the built bundle. Production must replace all
local secrets and use the approved IdP verifier; without it the API fails closed.

## Development

For component development:

```bash
cd web
npm run dev -- --host 127.0.0.1
```

The Vite development server does not define a local API proxy or inject identity. Use mocked
responses in component tests, or use the Compose same-origin stack for integrated work:

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
```

## Unit Tests

Run Vitest and Testing Library once, or watch during development:

```bash
cd web
npm test -- --run
npm test
```

Unit/component tests may mock HTTP at the browser boundary. They must still assert exact money
strings, URL year/quarter filters, blocked-versus-risk states, and formula evidence rendering.

## Lint and Typecheck

Run ESLint and the TypeScript compiler without emitting files:

```bash
cd web
npm run lint
npx tsc --noEmit
npx tsc -p tsconfig.e2e.json --noEmit
```

## Build

Create the production bundle. The script typechecks before invoking Vite:

```bash
cd web
npm run build
```

The container build uses `npm ci`, copies only `dist` into unprivileged Nginx, and serves on
port 8080. Keep `VITE_API_BASE_URL` empty for the Compose same-origin deployment.

## Browser E2E

Playwright does not start or seed services. First start Compose, then run the backend external
E2E seed from `infra/README.md`. That flow must complete against the real API, PostgreSQL,
Redis, and `worker-quarterly`, and it prints the unique standard company code.

Run the real-browser test without HTTP mocks:

```bash
cd web
export PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080
export E2E_STANDARD_COMPANY_CODE='<code printed by the external backend E2E>'
npm run test:e2e
```

The test file is `web/e2e/quarterly-dashboard.spec.ts`. A missing standard company code is a
configuration failure, not a skip. The test locates the seeded 2026 Q2 company, opens formula
details, and verifies persisted source values and versions through the running stack.

## Quarterly Dashboard

The dashboard is served at:

```text
http://127.0.0.1:8080/?fiscal_year=2026&quarter=2
```

Year and quarter filters are kept in URL search parameters. The page shows coverage, data
readiness, blocked companies, risk companies, the potential risk estimate, risk cases, and the formula
drawer. Money and rates arrive as exact API strings; the browser formats them but never
recomputes a tax formula. Formula details come from `GET /api/v1/detections/{id}` and retain
snapshot, source, tax-master, and rule-version lineage.

# Phase 4 Governance, Hardening, and Rollout Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the completed Phase 1–3 platform for controlled group-wide production use with one identity model, company-level isolation, immutable audit, secure exports, protected model access, measurable operations, signed releases, and a rehearsed rollback.

**Architecture:** Extend the existing modular monolith and its canonical Phase 1–3 paths. Authorization is enforced by API policy, PostgreSQL RLS, and the semantic evidence reader. Audit is append-only. Exports and model calls reapply server-side scope. Observability follows batch/company/period correlation. Production promotion requires a signed artifact manifest, historical replay, capacity evidence, UAT, and rollback proof.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy/Alembic, PostgreSQL RLS, Celery/Redis, OpenTelemetry, Prometheus-compatible metrics, structured JSON logs, React/TypeScript/Vite/Ant Design/TanStack Query, pytest/Hypothesis, Vitest/Playwright, Docker Compose, CI workflows.

---

## Canonical Paths and Migration Contract

This phase extends these existing paths and must not create competing identities or service layers:

```text
backend/src/tax_risk/
  security/principal.py                 # Phase 1 Principal, roles, organization path, company scope
  api/dependencies.py                   # Phase 1 authentication and database dependencies
  domain/cases.py                       # Shared risk fingerprint and lifecycle
  persistence/models.py                 # Shared ORM model registry
  persistence/repositories.py           # Shared transaction-scoped repositories
  application/master_data.py            # Phase 1 controlled tax master use case
  application/semantic/model_client.py  # Phase 2 provider-neutral StructuredModelClient
  api/routes/cases.py                    # Shared risk list/detail/action routes
  api/routes/dashboard.py                # Shared KPI queries
  main.py                                # FastAPI route registration
  workers/celery_app.py                  # Shared Celery registration
web/src/App.tsx                          # Shared UI route registration
```

Phase 4 adds the following linear migrations after Phase 3 head `0003`:

```text
0004_company_isolation.py    down_revision = "0003"
0005_audit_hardening.py      down_revision = "0004"
0006_export_jobs.py          down_revision = "0005"
0007_release_manifests.py    down_revision = "0006"
```

## Chunk 1: Authorization, Audit, Exports, and Model Security

### Task 1: Extend the single Principal and enforce company scope in API, PostgreSQL, and semantic evidence reads

**Files:**
- Modify: `backend/src/tax_risk/security/principal.py`
- Create: `backend/src/tax_risk/security/policies.py`
- Modify: `backend/src/tax_risk/api/dependencies.py`
- Modify: `backend/src/tax_risk/db.py`
- Create: `backend/src/tax_risk/application/semantic/evidence_reader.py`
- Create: `backend/migrations/versions/0004_company_isolation.py`
- Test: `backend/tests/unit/security/test_policies.py`
- Test: `backend/tests/integration/security/test_rls.py`
- Test: `backend/tests/integration/security/test_rls_pool_reset.py`
- Test: `backend/tests/integration/security/test_semantic_evidence_scope.py`

- [ ] **Step 1: Write failing authorization-matrix tests**

Test these approved boundaries without defining another `Principal`:

| Role | Allowed | Denied |
|---|---|---|
| Group tax | Group read, review, close, rule/model/account-dictionary release, master approval, scoped export, audit read | Source-interface administration |
| Division/region tax | Read authorized organization subtree | Review, close, release, master approval |
| Company finance | Read and process own-company cases, register correction voucher | Other-company access, final close, rule/model release |
| Data administrator | Source/interface maintenance and master-data import | Tax-risk conclusion, review, close, rule/model release |
| Auditor | Read versions, evidence, cases, and audit within assigned scope | Any write or export unless separately granted |

`RUN_MONITOR` belongs to group tax for all companies and to explicitly delegated operations service principals for one signed batch scope. API principals never inherit worker permissions. Quarterly, business-entertainment, and shared monthly-semantic workers receive distinct service identities restricted to their queue, run type, batch, companies, and periods. The export worker can read only the frozen authorized rows for its job; the model gateway can read only evidence references authorized for its one evaluation item.

The action enum must include `READ_RISK`, `PROCESS_COMPANY_RISK`, `CLOSE_RISK`, `RUN_MONITOR`, `MAINTAIN_SOURCE`, `IMPORT_MASTER`, `APPROVE_MASTER`, `MANAGE_RULE`, `PUBLISH_MODEL`, `EXPORT_RISK`, and `READ_AUDIT`.

- [ ] **Step 2: Run policy tests and verify RED**

Run: `cd backend && pytest tests/unit/security/test_policies.py -q`

Expected: FAIL because the complete policy module and actions do not exist.

- [ ] **Step 3: Implement policies by extending the Phase 1 identity**

Retain Phase 1 `subject`, `roles`, `allowed_company_ids`, and `organization_path`. Resolve organization descendants server-side into company IDs; never trust a client-supplied scope. Return 404 for an unauthorized resource ID and an empty set for unauthorized list rows. Worker calls use a signed service principal restricted to the batch company.

- [ ] **Step 4: Write failing RLS and semantic-reader tests**

Cover `ingest_batch`, `ingest_error`, company tax-master rows, `accounting_snapshot`, source voucher/business-document tables, `detection_record`, `risk_case`, `review_action`, `evidence_task`, `evidence_link`, `sap_link_coverage`, `monthly_semantic_scope_fact`, `audit_event`, and the semantic evidence projection. Assert:

- a deliberately unfiltered repository query cannot return another company;
- a forged company filter cannot broaden scope;
- `evidence.read_by_reference` rejects a reference owned by another company;
- group tax sees all companies, division/company roles see only assigned companies;
- `SET LOCAL` context is cleared when the pooled connection is returned;
- application and worker roles cannot bypass RLS.

Run: `cd backend && pytest tests/integration/security/test_rls.py tests/integration/security/test_rls_pool_reset.py tests/integration/security/test_semantic_evidence_scope.py -q`

Expected: FAIL because RLS, connection context, and the scoped evidence reader are absent.

- [ ] **Step 5: Implement RLS in Alembic and the scoped evidence reader**

Migration `0004` must enable and `FORCE ROW LEVEL SECURITY` on every company-scoped table. Direct-company tables compare `company_code` with the trusted session scope; child tables join to their company-owning parent. Set `app.subject`, `app.roles`, and `app.company_scope` with `SET LOCAL` inside each transaction and reset on pool check-in. Grant the application role neither table ownership nor `BYPASSRLS`.

V0.8 does not deploy an external vector or semantic index: semantic candidate and evidence retrieval remains a PostgreSQL projection protected by the same RLS and `EvidenceReader`. CI must assert no external semantic-index endpoint is configured and must run cross-company evidence-retrieval tests. Introducing an external index later requires a separate approved design, company namespace/filter enforcement, adversarial isolation tests, and backfill/revocation controls.

`EvidenceReader.read_by_reference(principal, reference_id)` performs policy authorization and a scoped repository read. The model gateway receives only returned evidence, never a database handle or free SQL tool.

- [ ] **Step 6: Run all authorization tests and migration checks**

Run:

```bash
cd backend
alembic upgrade 0004
pytest tests/unit/security tests/integration/security -q
alembic downgrade 0003 && alembic upgrade 0004
```

Expected: PASS; cross-company API, SQL, pooled-connection, worker, and semantic-evidence attempts return no protected data; downgrade/upgrade exits 0.

- [ ] **Step 7: Commit organization isolation**

```bash
git add backend/src/tax_risk/security backend/src/tax_risk/api/dependencies.py backend/src/tax_risk/db.py backend/src/tax_risk/application/semantic/evidence_reader.py backend/migrations/versions/0004_company_isolation.py backend/tests/unit/security backend/tests/integration/security
git commit -m "feat(auth): enforce company scope across data and evidence"
```

### Task 2: Make sensitive reads and writes attributable through immutable audit

**Files:**
- Modify: `backend/src/tax_risk/persistence/models.py`
- Create: `backend/migrations/versions/0005_audit_hardening.py`
- Create: `backend/src/tax_risk/application/audit.py`
- Create: `backend/src/tax_risk/api/routes/audit.py`
- Modify: `backend/src/tax_risk/api/routes/cases.py`
- Modify: `backend/src/tax_risk/application/master_data.py`
- Modify: `backend/src/tax_risk/main.py`
- Test: `backend/tests/unit/audit/test_audit_redaction.py`
- Test: `backend/tests/integration/audit/test_append_only.py`
- Test: `backend/tests/integration/audit/test_sensitive_actions.py`
- Test: `backend/tests/integration/api/test_audit_routes.py`

- [ ] **Step 1: Write failing audit coverage tests**

At this task boundary, require an event for login/authorization failure, risk list query, risk detail/evidence query, source upload, master import/approval, existing rule/model/account-dictionary release, and risk action/final close. Events contain actor, roles, company scope, action, target, request/batch ID, normalized-filter hash, returned-row count, related export/query ID when present, before/after summaries, result, reason code, and UTC time; free-text evidence and personal data are represented by stable references or redacted summaries. Task 3 adds export request/download events, Task 7 adds release/replay approval events, and Task 8 adds rollback events after those functions exist.

- [ ] **Step 2: Write failing append-only database tests**

Assert application-role `UPDATE` and `DELETE` fail; an insert cannot overwrite `occurred_at` or actor context; an authorized auditor can query only assigned companies; reading the audit endpoint does not recursively create an unbounded audit loop.

Run: `cd backend && pytest tests/unit/audit tests/integration/audit tests/integration/api/test_audit_routes.py -q`

Expected: FAIL because coverage, redaction, append-only enforcement, and routes are incomplete.

- [ ] **Step 3: Extend the existing Phase 1 audit model and database controls**

Do not create a second audit table. Add missing structured fields to `audit_event` through migration `0005`, an insert-only trigger, application-role grants, and indexes on time, actor, company, action, and target. Authorization failures are written in an independent security-audit transaction because the rejected business transaction is rolled back. Never rewrite the already-executed `0004` migration.

- [ ] **Step 4: Add one audit application service and read-only route**

`application/audit.py` owns redaction, append, and scoped search. Integrate it at service/route boundaries named in Step 1. `GET /api/v1/audit-events` requires `READ_AUDIT`, applies both API scope and RLS, paginates, and excludes unredacted request bodies.

- [ ] **Step 5: Run audit tests and migration regression**

Run: `cd backend && alembic upgrade 0005 && pytest tests/unit/audit tests/integration/audit tests/integration/api/test_audit_routes.py -q && alembic current`

Expected: PASS; every action implemented through this task has exactly one primary event, forbidden mutations fail, and Alembic reports `0005`. The full future-action matrix is not claimed until Task 9.

- [ ] **Step 6: Commit immutable audit coverage**

```bash
git add backend/src/tax_risk/persistence/models.py backend/migrations/versions/0005_audit_hardening.py backend/src/tax_risk/application/audit.py backend/src/tax_risk/api/routes/audit.py backend/src/tax_risk/api/routes/cases.py backend/src/tax_risk/application/master_data.py backend/src/tax_risk/main.py backend/tests/unit/audit backend/tests/integration/audit backend/tests/integration/api/test_audit_routes.py
git commit -m "feat(audit): preserve sensitive reads and decisions"
```

### Task 3: Generate permission-scoped asynchronous exports and recheck access at download

**Files:**
- Create: `backend/src/tax_risk/domain/exports.py`
- Create: `backend/src/tax_risk/application/exports.py`
- Create: `backend/src/tax_risk/workers/exports.py`
- Modify: `backend/src/tax_risk/application/business_entertainment/export.py`
- Modify: `backend/src/tax_risk/api/routes/exports.py`
- Modify: `backend/src/tax_risk/persistence/models.py`
- Modify: `backend/src/tax_risk/persistence/repositories.py`
- Modify: `backend/src/tax_risk/workers/celery_app.py`
- Modify: `backend/src/tax_risk/main.py`
- Create: `backend/migrations/versions/0006_export_jobs.py`
- Create: `web/src/features/exports/ExportJobsPage.tsx`
- Create: `web/src/features/exports/ExportJobsPage.test.tsx`
- Modify: `web/src/App.tsx`
- Test: `backend/tests/integration/exports/test_export_scope.py`
- Test: `backend/tests/integration/exports/test_download_reauthorization.py`
- Test: `backend/tests/integration/exports/test_export_audit.py`
- Test: `backend/tests/unit/exports/test_spreadsheet_safety.py`

- [ ] **Step 1: Write failing frozen-scope and current-permission tests**

Assert job creation intersects requested filters with server scope, the worker uses the frozen authorized scope, an unscoped repository call cannot broaden rows because RLS remains active, and download rechecks current permission. A user whose access was revoked after generation receives 404 and no object URL. `test_export_audit.py` requires create, completion/failure, download, denial, and expiry events with normalized-filter hash, row count, checksum, and export job ID but no workbook content.

- [ ] **Step 2: Write failing spreadsheet-safety and lifecycle tests**

Text cells beginning with `=`, `+`, `-`, or `@` are prefixed with a single quote; true numeric cells remain numeric, including negative amounts. Test queued/running/completed/failed/expired states, checksum, row count, schema version, expiry, object key, and object deletion/revocation after expiry.

Run: `cd backend && pytest tests/integration/exports tests/unit/exports -q`

Expected: FAIL because the export domain, worker, migration, and safety policy do not exist.

- [ ] **Step 3: Implement migration `0006`, domain states, repository, and use case**

Store request actor, role/scope snapshot, normalized filters, current authorization version, schema version, status, row count, SHA-256, object key, expiry, and failure code. Object keys are generated server-side. `create_export`, `render_export`, and `authorize_download` use the shared policy service and audit service. Refactor the Phase 2 synchronous business-entertainment exporter to provide only a row/schema producer to this generic job service; do not keep a second authorization or artifact-delivery path.

- [ ] **Step 4: Register the worker and endpoints**

Register the export task in `workers/celery_app.py`; register create/status/download routes in `main.py`. A completed download returns a short-lived URL only after current permission and company-scope revalidation. Never store a client-provided URL.

- [ ] **Step 5: Add the export page and component tests**

The page shows state, normalized scope, row count, checksum, expiry, and safe failure reason. It hides download after permission revocation or expiry and never exposes object-storage credentials.

Run: `cd web && npm test -- --run src/features/exports/ExportJobsPage.test.tsx && npm run build`

Expected: PASS; component tests and build exit 0.

- [ ] **Step 6: Run backend export tests and migration continuity**

Run: `cd backend && alembic upgrade 0006 && pytest tests/integration/exports tests/unit/exports -q`

Expected: PASS; revoked users cannot download, cross-company rows are absent, text formula injection is neutralized, and negative numeric values remain numeric.

- [ ] **Step 7: Commit secure exports**

```bash
git add backend/src/tax_risk/domain/exports.py backend/src/tax_risk/application/exports.py backend/src/tax_risk/application/business_entertainment/export.py backend/src/tax_risk/workers backend/src/tax_risk/api/routes/exports.py backend/src/tax_risk/persistence backend/src/tax_risk/main.py backend/migrations/versions/0006_export_jobs.py backend/tests/integration/exports backend/tests/unit/exports web/src/features/exports web/src/App.tsx
git commit -m "feat(exports): scope and reauthorize risk downloads"
```

### Task 4: Route all semantic calls through a protected enterprise model gateway

**Files:**
- Create: `backend/src/tax_risk/model_gateway/policy.py`
- Create: `backend/src/tax_risk/model_gateway/service.py`
- Modify: `backend/src/tax_risk/application/semantic/model_client.py`
- Modify: `backend/src/tax_risk/application/semantic/evidence_reader.py`
- Modify: `backend/src/tax_risk/application/business_entertainment/agent.py`
- Modify: `backend/src/tax_risk/application/semantic/sap_voucher_agent.py`
- Modify: `backend/src/tax_risk/application/welfare/service.py`
- Modify: `backend/src/tax_risk/application/donation/service.py`
- Modify: `backend/src/tax_risk/adapters/model/enterprise_structured_client.py`
- Test: `backend/tests/unit/model_gateway/test_payload_policy.py`
- Test: `backend/tests/unit/model_gateway/test_structured_response.py`
- Test: `backend/tests/unit/model_gateway/test_no_direct_adapter_imports.py`
- Test: `backend/tests/integration/model_gateway/test_evidence_authorization.py`
- Test: `backend/tests/security/test_prompt_injection.py`

- [ ] **Step 1: Write failing data-minimization and provider-policy tests**

Assert non-allowlisted identity, phone, bank, and attachment fields are removed; only necessary purpose, counterparty type, participant category, scene, amount, and cited spans remain. Reject a production provider configuration unless enterprise no-public-training and approved retention settings are present.

- [ ] **Step 2: Write failing prompt-injection, tool, and company-scope tests**

Treat OA/Hesi/SAP text requesting SQL, a new tool, another company, hidden instructions, or schema changes as quoted evidence. Assert the gateway exposes only `evidence.read_by_reference`; the evidence reader rechecks the Principal and company; model output cannot change canonical identity, amount, source mode, link quality, or company.

Run: `cd backend && pytest tests/unit/model_gateway tests/integration/model_gateway tests/security/test_prompt_injection.py -q`

Expected: FAIL because the protected gateway policy is absent.

- [ ] **Step 3: Implement policy, gateway, and strict response assembly**

The gateway accepts server-owned context and the Phase 2 `StructuredModelClient`; it prepares an allowlisted payload, records provider/model/prompt/case-library versions, invokes the adapter, validates the model-only judgment schema, and lets the server assemble the final detection. Reject non-allowlisted tool requests and schema failures. A second schema failure creates a technical-review item, not a safe result or tax risk.

- [ ] **Step 4: Remove direct provider paths and add audited metadata**

Business entertainment and the shared welfare/donation SAP-voucher Agent call the gateway; their service factories inject no enterprise adapter directly. Add an AST-based architecture test that fails when any module outside `model_gateway/service.py` imports or constructs `EnterpriseStructuredClient`. Record model adapter, version IDs, token counts, latency, policy result, request hash, and error code without full free text. Store no public-training consent and no provider credential in audit/log rows.

- [ ] **Step 5: Run the model and accumulated semantic suites**

Run:

```bash
cd backend
pytest tests/unit/model_gateway tests/integration/model_gateway tests/security/test_prompt_injection.py -q
pytest tests/unit/business_entertainment tests/unit/semantic tests/evaluation -q
```

Expected: PASS; malicious text cannot broaden tools or companies, server-owned fields are unchanged, and all three monitor gold sets remain green.

- [ ] **Step 6: Commit model-gateway controls**

```bash
git add backend/src/tax_risk/model_gateway backend/src/tax_risk/application/semantic backend/src/tax_risk/application/business_entertainment/agent.py backend/src/tax_risk/application/welfare/service.py backend/src/tax_risk/application/donation/service.py backend/src/tax_risk/adapters/model/enterprise_structured_client.py backend/tests/unit/model_gateway backend/tests/integration/model_gateway backend/tests/security/test_prompt_injection.py
git commit -m "feat(ai-security): constrain enterprise model data and tools"
```

## Chunk 2: Operations, Release Evidence, and Reversible Rollout

### Task 5: Add correlated logs, metrics, traces, health checks, and an operations view

**Files:**
- Create: `backend/src/tax_risk/observability/context.py`
- Create: `backend/src/tax_risk/observability/metrics.py`
- Create: `backend/src/tax_risk/observability/tracing.py`
- Modify: `backend/src/tax_risk/api/routes/health.py`
- Modify: `backend/src/tax_risk/main.py`
- Modify: `backend/src/tax_risk/workers/celery_app.py`
- Create: `infra/observability/otel-collector.yaml`
- Create: `infra/observability/dashboard.json`
- Create: `web/src/features/operations/OperationsDashboard.tsx`
- Create: `web/src/features/operations/OperationsDashboard.test.tsx`
- Modify: `web/src/App.tsx`
- Test: `backend/tests/unit/observability/test_context.py`
- Test: `backend/tests/integration/observability/test_health.py`
- Test: `backend/tests/integration/observability/test_metrics.py`

- [ ] **Step 1: Write failing correlation and metric tests**

Every API/worker log and trace must carry request ID or task ID plus batch, company, fiscal year, and period when available. Metrics cover source readiness, quality blocks, company task outcome, formula runtime, semantic candidates/detections/errors, linkage coverage, evidence backlog, case age, exports, authorization failures, data-ready time, and output-ready time. Company names and free text are forbidden metric labels.

- [ ] **Step 2: Write failing liveness/readiness tests**

Liveness checks only process responsiveness. Readiness checks PostgreSQL, Redis, object storage, the configured expected migration head, active rule/version manifests, and model-gateway configuration without calling the external model. During this task the expected head is `0006`; Task 7 changes production configuration and tests to `0007`. A dependency failure returns 503 with stable component codes.

Run: `cd backend && pytest tests/unit/observability tests/integration/observability -q`

Expected: FAIL because context propagation, metrics, and readiness are absent.

- [ ] **Step 3: Implement telemetry and dependency health**

Propagate context through FastAPI middleware and Celery headers. Export structured JSON logs, traces, counters, and histograms. Define `data_ready_at` as the immutable `SnapshotSet.published_at` written when every required source member passes the quality gate. Persist `batch_finished_at` when all company tasks reach any terminal state, but persist `company_output_ready_at` only after that valid company's detections, coverage/evidence tasks, and risks commit with `SUCCEEDED`. A technical failure has null `company_output_ready_at` even when the batch is `PARTIAL_SUCCESS`; the batch-level `output_ready_at` is the maximum company timestamp only when every valid company succeeded. Phase 1 owns `SnapshotSet.published_at`; Phase 2/3 must never substitute upload time or model-call time.

- [ ] **Step 4: Add the operations dashboard**

Show data errors, technical failures, and tax risks separately. Include batch/company status, queue age, delivery-lag countdown, provider failures, linkage coverage, evidence backlog, and retry controls restricted by `RUN_MONITOR`.

Run: `cd web && npm test -- --run src/features/operations/OperationsDashboard.test.tsx && npm run build`

Expected: PASS; operations UI distinguishes the three issue classes and shows no sensitive free text in charts.

- [ ] **Step 5: Run telemetry and route regression**

Run: `cd backend && pytest tests/unit/observability tests/integration/observability tests/integration/api -q`

Expected: PASS; context survives API-to-worker boundaries, readiness never invokes the model, and dependency codes are stable.

- [ ] **Step 6: Commit operational visibility**

```bash
git add backend/src/tax_risk/observability backend/src/tax_risk/api/routes/health.py backend/src/tax_risk/main.py backend/src/tax_risk/workers/celery_app.py backend/tests/unit/observability backend/tests/integration/observability infra/observability web/src/features/operations web/src/App.tsx
git commit -m "feat(ops): expose monitored batch health"
```

### Task 6: Prove quarterly and all monthly monitors isolate failures, rerun safely, and meet the 100+ company window

**Files:**
- Create: `backend/src/tax_risk/domain/task_runs.py`
- Modify: `backend/src/tax_risk/workers/quarterly_batch.py`
- Modify: `backend/src/tax_risk/workers/business_entertainment.py`
- Modify: `backend/src/tax_risk/workers/monthly_semantic.py`
- Create: `backend/tests/unit/workers/test_task_run_contract.py`
- Create: `backend/tests/load/profiles/126_companies.json`
- Create: `backend/tests/load/conftest.py`
- Create: `backend/tests/load/test_capacity_profile.py`
- Create: `backend/tests/load/test_failure_isolation.py`
- Create: `backend/tests/load/test_replay_idempotency.py`
- Create: `backend/tests/load/test_t_plus_2.py`

- [ ] **Step 1: Define and validate the fixed acceptance profile**

The profile contains 126 companies, one quarterly snapshot per company, 1,000 monthly SAP/OA/Hesi detail rows per company across the three monitors, 16 worker processes, one forced source failure, one retryable provider failure, and one non-retryable master-data error. A **valid company** is one whose required source members, controlled company list where applicable, and approved master/version inputs are complete at `SnapshotSet.published_at`; source and master blockers are reported separately and excluded from the valid-company success and T+2 denominators, never counted as safe. A provider failure occurs after valid input and remains in both denominators. The report records total, valid, blocked, technically failed, and succeeded companies, CPU/memory profile, concurrency, timestamps, task counts, rows, retries, and peak queue age.

- [ ] **Step 2: Write failing isolation and idempotency tests**

Write `test_task_run_contract.py` first. `TaskRunResult` contains run type, monitor type, batch, company, period, idempotency key, terminal state, retry count, timestamps, and stable error code. Lock these keys:

- quarterly: `company|fiscal_year|quarter|snapshot_set|rule_version`;
- business entertainment: `company|fiscal_year|through_month|snapshot_set|company_list|rule|model|prompt|case_library|account_dictionary`;
- welfare or donation: `company|fiscal_year|through_month|monitor_type|snapshot_set|rule|model|prompt|case_library|account_dictionary`.

Assert at least 98% valid-company success; company failure never rolls back another company; only retryable failures receive bounded exponential retries; failed companies can rerun alone. Replaying identical key inputs preserves one case per fingerprint, one candidate/evidence task per key, and one active amount after a business-document-to-SAP merge. Changing any governed version creates a distinct run without changing the stable risk fingerprint.

- [ ] **Step 3: Write failing capacity and T+2 tests**

On the documented 8-vCPU/16-GB reference runner with concurrency 16, the 126-company profile must finish within 24 hours. Every valid company must reach `SUCCEEDED` and receive `company_output_ready_at` within 48 hours of its persisted data-ready timestamp; a provider failure that is not recovered successfully by the deadline makes the T+2 gate fail even if aggregate success remains at least 98%. A synthetic clock verifies the exact 48-hour boundary independently of wall-clock test duration.

Run: `cd backend && pytest tests/load/test_failure_isolation.py tests/load/test_replay_idempotency.py tests/load/test_t_plus_2.py -q`

Expected: FAIL until all worker paths use stable idempotency keys, isolated transactions, bounded retries, and delivery timestamps.

- [ ] **Step 4: Implement shared task outcome and bounded-retry behavior**

Implement the shared result envelope and exact keys in `domain/task_runs.py`, then use them in all four monitor task paths. Welfare and donation share `workers/monthly_semantic.py` but keep distinct `monitor_type` keys and outcomes. Technical retryable errors use capped exponential backoff; source/master/business errors are blocked without automatic retry. Persist per-company outcome before fan-in aggregation and allow company-only rerun. `backend/tests/load/conftest.py` must register `--capacity-report`, validate its parent directory, and write the documented JSON schema even when a gate fails.

- [ ] **Step 5: Run the full profile and write the capacity artifact**

Run: `cd backend && pytest tests/load -q --capacity-report=../artifacts/acceptance/phase-4/capacity-report.json`

Expected: PASS; success is at least 98%, forced failures are isolated, duplicate active exposure count is zero, elapsed time is at most 24 hours on the reference profile, every valid company has a successful output within 48 hours, and failed/partial tasks never receive a false output-ready timestamp.

- [ ] **Step 6: Commit resilience and capacity proof**

```bash
git add backend/src/tax_risk/domain/task_runs.py backend/src/tax_risk/workers backend/tests/unit/workers/test_task_run_contract.py backend/tests/load
git commit -m "test(ops): prove group batch resilience and timeliness"
```

### Task 7: Build signed artifact manifests, replay gates, CI, and executable verification targets

**Files:**
- Create: `backend/src/tax_risk/release/manifest.py`
- Create: `backend/src/tax_risk/release/signing.py`
- Create: `backend/src/tax_risk/release/replay_runner.py`
- Create: `backend/src/tax_risk/release/replay_gate.py`
- Create: `backend/src/tax_risk/release/reporting.py`
- Create: `backend/src/tax_risk/adapters/signing/kms_ed25519_signer.py`
- Modify: `backend/src/tax_risk/persistence/models.py`
- Create: `backend/migrations/versions/0007_release_manifests.py`
- Create: `backend/tests/unit/release/test_manifest.py`
- Create: `backend/tests/unit/release/test_signature.py`
- Create: `backend/tests/integration/release/test_kms_signer.py`
- Create: `backend/tests/integration/release/test_replay_gate.py`
- Create: `backend/tests/integration/release/test_release_audit.py`
- Create: `Makefile`
- Modify: `web/playwright.config.ts`
- Create: `infra/scripts/verify_governance.sh`
- Create: `infra/scripts/verify_release.sh`
- Create: `infra/scripts/verify_capacity.sh`
- Create: `infra/scripts/verify_migrations.sh`
- Create: `infra/scripts/security_check.sh`
- Create: `infra/scripts/run_uat.sh`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Create: `infra/runbooks/release.md`

- [ ] **Step 1: Write failing canonical-manifest and signature tests**

The canonical JSON manifest contains application image digest, Git commit, migration head, rule package, prompt package, model adapter/config version, account dictionary, case library, and evaluation/replay report hashes. Verify an Ed25519 signature with the approved public key; changing any byte, artifact hash, or migration head fails verification. Production signing calls the approved KMS/HSM through workload identity, an environment allowlist of key IDs, and an auditable sign operation; private key material never enters the process. Tests use an ephemeral key plus a fake KMS endpoint.

- [ ] **Step 2: Write failing deterministic and semantic replay-gate tests**

Block release unless formula/oracle accuracy is 100%, traceability is 100%, master-data defects are blocked 100%, known semantic cases have zero misses, pilot recall is at least 90%, formal-production recall is at least 95%, high-confidence accuracy is at least 80%, group batch success is at least 98%, and security/migration/rollback checks pass. `test_release_audit.py` requires candidate creation, replay start/result, approval/rejection, signing, verification, and promotion events with manifest/report hashes and approver identity.

Run: `cd backend && pytest tests/unit/release tests/integration/release -q`

Expected: FAIL because manifest, signing, persistence, and replay gates do not exist.

- [ ] **Step 3: Implement single-responsibility release modules, KMS signing, and migration `0007`**

`manifest.py` canonicalizes and hashes; `signing.py` defines signer/verifier ports and the public-key verifier; `replay_runner.py` runs frozen snapshots; `replay_gate.py` evaluates thresholds; `reporting.py` writes JSON and human-readable reports. `kms_ed25519_signer.py` obtains a short-lived workload token, verifies the requested key ID is allowlisted, asks KMS/HSM to sign the canonical digest, and returns signature plus key/version ID. Verification trusts the active and explicitly retained previous public key during a documented rotation overlap; unknown or retired IDs fail closed. Persist manifest hash, signature, signer key/version ID, artifact references, approvals, and replay report without storing a private key.

- [ ] **Step 4: Add executable Make targets before using them**

Create these exact target mappings; every script uses `set -euo pipefail`, creates its output directory, validates the emitted JSON/JUnit file, and exits nonzero on a failed gate:

| Make target | Checked-in command | Required artifact |
|---|---|---|
| `test-backend` | `cd backend && pytest --junitxml=../artifacts/acceptance/backend.xml` | `backend.xml` |
| `test-web` | `cd web && npm test -- --run && npm run build && PLAYWRIGHT_JSON_OUTPUT_NAME=../artifacts/acceptance/web-test-results/results.json npx playwright test --reporter=json` | `artifacts/acceptance/web-test-results/results.json` |
| `verify-governance` | `infra/scripts/verify_governance.sh` | `phase-4/governance.xml` |
| `verify-release` | `infra/scripts/verify_release.sh` | `phase-4/replay-report.json` and signed manifest |
| `verify-capacity` | `infra/scripts/verify_capacity.sh` | `phase-4/capacity-report.json` |
| `verify-migrations` | `infra/scripts/verify_migrations.sh` | `phase-4/migrations.json` |
| `security-check` | `infra/scripts/security_check.sh` | `phase-4/security.json` |
| `uat` | `infra/scripts/run_uat.sh` | `phase-4/uat-scorecard.json` |
| `verify-rollback` | `infra/scripts/rollback_drill.sh` from Task 8 | `phase-4/rollback-report.json` |

- [ ] **Step 5: Add CI and signed-release workflows**

CI runs backend, frontend, type/lint, dependency/security, migration-from-empty, migration-from-`0003`, RLS, PostgreSQL semantic-evidence retrieval isolation, the no-external-index configuration assertion, and E2E tests. Release verifies the candidate manifest before replay, signs only after approval, uploads manifest/signature/reports, and re-verifies downloaded artifacts before promotion.

- [ ] **Step 6: Run release tests and target smoke checks**

Run:

```bash
cd backend && alembic upgrade 0007 && pytest tests/unit/release tests/integration/release -q
cd .. && make verify-governance && make verify-release
```

Expected: PASS; tampering tests fail closed; valid manifest verifies; replay report is written to `artifacts/acceptance/phase-4/replay-report.json`.

- [ ] **Step 7: Commit signed release gates**

```bash
git add backend/src/tax_risk/release backend/src/tax_risk/adapters/signing backend/src/tax_risk/persistence/models.py backend/migrations/versions/0007_release_manifests.py backend/tests/unit/release backend/tests/integration/release Makefile web/playwright.config.ts .github/workflows infra/scripts/verify_governance.sh infra/scripts/verify_release.sh infra/scripts/verify_capacity.sh infra/scripts/verify_migrations.sh infra/scripts/security_check.sh infra/scripts/run_uat.sh infra/runbooks/release.md
git commit -m "ci: gate releases on signed replay evidence"
```

### Task 8: Automate rollback drills, pilot acceptance, and wave-based rollout

**Files:**
- Create: `backend/src/tax_risk/release/scorecard.py`
- Create: `backend/tests/unit/release/test_scorecard.py`
- Create: `backend/tests/integration/release/test_rollback_drill.py`
- Create: `backend/tests/integration/release/test_rollback_audit.py`
- Create: `infra/scripts/rollback_drill.sh`
- Create: `infra/runbooks/rollback.md`
- Create: `infra/runbooks/data-source-failure.md`
- Create: `infra/runbooks/model-provider-failure.md`
- Create: `infra/runbooks/pilot-uat.md`
- Create: `infra/runbooks/group-rollout.md`
- Create: `docs/operations/acceptance-scorecard.md`
- Create: `docs/operations/data-owner-checklist.md`
- Create: `docs/operations/user-training.md`

- [ ] **Step 1: Write failing production-scorecard tests**

Require evidence references for formula accuracy 100%, traceability 100%, master-data blocking 100%, valid-company success at least 98%, formal recall at least 95%, high-confidence accuracy at least 80%, known-case misses zero, monthly delivery at most 48 hours, authorization/RLS/semantic-evidence retrieval isolation, no external index configuration, audit immutability, verified signature, restore, and rollback. Missing evidence makes `production_ready` false.

- [ ] **Step 2: Write failing rollback fault-injection, idempotency, and resume tests**

Inject an invalid model configuration, a killed worker with in-flight tasks, a revoked user with a completed export, and an incompatible candidate artifact. Assert the drill drains or safely revokes tasks, records affected batch IDs, revokes downloads, selects the previous verified manifest, restores/downgrades only on a disposable restore copy, redeploys, verifies source/snapshot/risk checksums, and reruns one representative company without duplicate exposure. Run the same approved inputs twice and assert no second restore, revoke, deployment, or case is created. Inject a failure after every stage, then resume from the persisted checkpoint and prove completed stages are verified and skipped while the remaining stages finish. `test_rollback_audit.py` requires request/approval, every checkpoint transition, failure/resume, manifest switch, checksum result, representative rerun, and recovery decision events.

Run: `cd backend && pytest tests/unit/release/test_scorecard.py tests/integration/release/test_rollback_drill.py -q`

Expected: FAIL because the scorecard and repeatable rollback drill are absent.

- [ ] **Step 3: Implement the scorecard and exact rollback script**

`rollback_drill.sh` accepts candidate manifest, previous manifest, backup ID, affected batch IDs, environment, approved change ID, and checkpoint path. It performs preflight signature verification, task drain/revoke, export revocation, backup restore to an isolated target, optional tested migration downgrade, previous application/artifact deployment, checksum comparison, representative rerun, and JSON report emission. Each stage writes an idempotency key, input hash, terminal state, and evidence before advancing; rerun validates completed evidence and resumes the first incomplete stage. Every destructive production operation requires the approved change ID and environment guard.

- [ ] **Step 4: Write operational runbooks with commands and owners**

Each runbook names signal, decision owner, approval, containment command, rollback command, data-consistency check, communication, recovery proof, and artifact path. Model failure preserves candidates for later judgment and never marks them safe. Source failure creates a data exception and never outputs “no risk.”

- [ ] **Step 5: Execute pilot UAT on frozen snapshots**

Pilot sequence: internal test companies, selected companies covering profit/loss and linkage modes, one full-quarter parallel run, then organization waves. Finance and tax double-check standard formulas and sampled semantic cases; data owners sign reconciliation; security/operations sign isolation, restore, and rollback evidence.

Run: `make verify-rollback && make uat SNAPSHOT_SET=pilot-2026q2`

Expected: PASS; `rollback-report.json` has `recovery_verified=true`; `uat-scorecard.json` records all thresholds and approvers and has `production_ready=true` only when every gate passes.

- [ ] **Step 6: Commit rollout controls**

```bash
git add backend/src/tax_risk/release/scorecard.py backend/tests/unit/release/test_scorecard.py backend/tests/integration/release/test_rollback_drill.py infra/scripts infra/runbooks docs/operations
git commit -m "docs(rollout): make tax monitoring release reversible"
```

### Task 9: Run and record the final full-system verification

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/acceptance-scorecard.md`
- Create: `backend/tests/integration/audit/test_full_action_matrix.py`

- [ ] **Step 1: Verify the complete audit action matrix**

Run: `cd backend && pytest tests/integration/audit/test_full_action_matrix.py -q`

Expected: every sensitive read/write plus export, release/replay, and rollback action produces the required redacted event; all matrix rows are covered and no future action is marked covered by a stub.

- [ ] **Step 2: Verify backend, frontend, and end-to-end behavior**

Run: `make test-backend && make test-web`

Expected: all pytest, Vitest, build, and Playwright checks exit 0.

- [ ] **Step 3: Verify authorization, security, and migrations**

Run: `make verify-governance && make security-check && make verify-migrations`

Expected: RLS/API/semantic-evidence adversarial tests and the no-external-index assertion pass; no unresolved high-severity dependency or static finding; empty and `0003` databases upgrade to `0007`; documented disposable downgrade succeeds.

- [ ] **Step 4: Verify replay, capacity, timeliness, and rollback**

Run: `make verify-release && make verify-capacity COMPANY_FIXTURE=126 && make verify-rollback`

Expected: signature and replay gate approved; valid-company success is at least 98%; capacity profile is within 24 hours; monthly delivery is within 48 hours; rollback recovery is verified.

- [ ] **Step 5: Verify pilot evidence and manifest completeness**

Run: `make uat SNAPSHOT_SET=pilot-2026q2`

Expected: formula accuracy, traceability, and master-data blocking are 100%; formal recall is at least 95%; high-confidence accuracy is at least 80%; known-case misses are zero; `production_ready=true`.

- [ ] **Step 6: Record evidence links and commit the handoff**

Update the README with start, monitoring, backup, restore, release, rollback, and escalation commands. Link every acceptance artifact and approval in the scorecard.

```bash
git add README.md docs/operations/acceptance-scorecard.md backend/tests/integration/audit/test_full_action_matrix.py artifacts/acceptance
git commit -m "docs: hand off verified tax monitoring operations"
```

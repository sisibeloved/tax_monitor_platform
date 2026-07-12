# Group Income Tax Risk Monitoring Platform Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the confirmed V0.8 platform for more than 100 companies through four independently verifiable phases while preserving formula accuracy, evidence traceability, semantic-Agent boundaries, and reversible rollout.

**Architecture:** Build one modular monolith with batch ingestion, versioned master data, immutable snapshots, deterministic quarterly rules, provider-neutral semantic Agents, unified risk cases, and human review. Execute the linked phase plans in order. A small acceptance harness records every phase command, output hash, Git revision, threshold result, and artifact; missing or invalid evidence blocks promotion.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy/Alembic, PostgreSQL, Celery/Redis, pytest/Hypothesis, React/TypeScript/Vite/Ant Design/TanStack Query, Vitest/Playwright, OpenTelemetry, Docker Compose, CI workflows.

---

## Chunk 1: Four-Phase Delivery and Acceptance Orchestration

### Task 1: Build the acceptance harness and lock cross-phase contracts

**Files:**
- Create: `scripts/acceptance/record_command.py`
- Create: `scripts/acceptance/verify_artifact.py`
- Create: `scripts/acceptance/verify_contract_registry.py`
- Create: `scripts/acceptance/tests/test_acceptance_tools.py`
- Create: `scripts/acceptance/tests/fixtures/valid_command_result.json`
- Create: `scripts/acceptance/tests/fixtures/invalid_command_result.json`
- Create: `artifacts/acceptance/.gitkeep`

Authoritative inputs:

- `docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md`
- `docs/design/architecture/2026-07-12-group-income-tax-risk-monitoring-platform-architecture.md`
- `docs/design/function/2026-07-12-group-income-tax-risk-monitoring-platform-function.md`
- `docs/design/detailed/2026-07-12-group-income-tax-risk-monitoring-platform-detailed.md`

The V0.8 specification wins when documents differ. Formula, threshold, source-system, evidence-priority, or automation-boundary changes require an approved specification revision before code changes.

Canonical shared paths:

| Responsibility | Canonical path |
|---|---|
| Principal and organization scope | `backend/src/tax_risk/security/principal.py` |
| FastAPI dependencies | `backend/src/tax_risk/api/dependencies.py` |
| Risk fingerprint and lifecycle | `backend/src/tax_risk/domain/cases.py` |
| Application use cases | `backend/src/tax_risk/application/` |
| ORM models and repositories | `backend/src/tax_risk/persistence/models.py`, `backend/src/tax_risk/persistence/repositories.py` |
| Provider-neutral model port | `backend/src/tax_risk/application/semantic/model_client.py` |
| Risk and dashboard routes | `backend/src/tax_risk/api/routes/cases.py`, `backend/src/tax_risk/api/routes/dashboard.py` |
| Route and worker registration | `backend/src/tax_risk/main.py`, `backend/src/tax_risk/workers/celery_app.py` |
| Frontend root | `web/src/App.tsx` |

Canonical linear migration registry:

| Phase | Migration | `down_revision` |
|---|---|---|
| 1 | `0001_control_plane.py` | base |
| 2 | `0002a_business_entertainment_scope.py` | `0001` |
| 2 | `0002b_business_entertainment_observations.py` | `0002a` |
| 2 | `0002c_semantic_contracts_accounts.py` | `0002b` |
| 2 | `0002d_semantic_artifacts_calls.py` | `0002c` |
| 3 | `0003_welfare_donation_agents.py` | `0002d` |
| 4 | `0004_company_isolation.py` | `0003` |
| 4 | `0005_audit_hardening.py` | `0004` |
| 4 | `0006_export_jobs.py` | `0005` |
| 4 | `0007_release_manifests.py` | `0006` |

- [ ] **Step 1: Write failing acceptance-tool tests**

Test that `record_command.py` runs an argv array without shell interpolation, writes a companion log and JSON containing name, argv, UTC start/end, exit code, Git revision, stdout/stderr SHA-256, and referenced input hashes, then returns the child exit code. Test that `verify_artifact.py` rejects missing files, nonzero exit, hash mismatch, or missing required fields. Test the contract registry against temporary valid and broken plan/migration fixtures.

- [ ] **Step 2: Run the harness tests and verify RED**

Run: `python3 -m unittest discover -s scripts/acceptance/tests -p 'test_*.py'`

Expected: FAIL because the three acceptance modules do not exist.

- [ ] **Step 3: Implement the three standard-library tools**

`record_command.py` must use `subprocess.run` with an argv list, never `shell=True`. Every JSON/log artifact is flushed to a same-directory temporary file, `fsync`ed, then atomically renamed; an interruption must leave either the previous complete artifact or no final artifact. `verify_artifact.py` validates command-result JSON and any required JUnit/JSON companions. `verify_contract_registry.py` parses the five plan files and later the Alembic directory, fails on duplicate canonical responsibilities or a non-linear migration chain, and writes the checked paths, revisions, input hashes, and result to its output.

- [ ] **Step 4: Run tests and generate the plans-only registry artifact**

Run:

```bash
python3 -m unittest discover -s scripts/acceptance/tests -p 'test_*.py'
python3 scripts/acceptance/verify_contract_registry.py \
  --plans-dir docs/superpowers/plans \
  --mode plans-only \
  --output artifacts/acceptance/contract-registry.json
python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/contract-registry.json
```

Expected: all commands exit 0; the registry records `0001 -> 0002a -> 0002b -> 0002c -> 0002d -> 0003 -> 0004 -> 0005 -> 0006 -> 0007` and all canonical paths.

- [ ] **Step 5: Commit the acceptance harness**

```bash
git add scripts/acceptance artifacts/acceptance/.gitkeep artifacts/acceptance/contract-registry.json
git commit -m "test(acceptance): record phased delivery evidence"
```

### Task 2: Implement and accept Phase 1 deterministic monitoring

**Files:**
- Execute: `docs/superpowers/plans/2026-07-12-phase-1-foundation-quarterly.md`
- Generate: `artifacts/acceptance/phase-1/formula-report.xml`
- Generate: `artifacts/acceptance/phase-1/formula-command.json`
- Generate: `artifacts/acceptance/phase-1/full-stack.xml`
- Generate: `artifacts/acceptance/phase-1/full-stack-command.json`

- [ ] **Step 1: Execute every Phase 1 task and commit checkpoint**

Do not proceed until the Phase 1 plan is complete. Formula accuracy, traceability, master-data quality blocking, one-company failure isolation, and absence of semantic-model dependencies are blocking contracts.

- [ ] **Step 2: Record the deterministic formula gate**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-1-formulas \
  --output artifacts/acceptance/phase-1/formula-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py tests/unit/domain/test_quarterly_*.py -q --junitxml=../artifacts/acceptance/phase-1/formula-report.xml'
```

Expected: exit 0; all approved examples, ledger rounding, exact 5-percentage-point boundary, negative pre-floor potential-base cases, and property tests pass.

- [ ] **Step 3: Record the integration, 105-company, and browser gate**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-1-full-stack \
  --output artifacts/acceptance/phase-1/full-stack-command.json \
  -- bash -lc 'cd backend && pytest tests/integration tests/e2e/test_quarterly_standard_scenario.py -q --junitxml=../artifacts/acceptance/phase-1/full-stack.xml && cd ../web && npm test -- --run && npx playwright test e2e/quarterly-dashboard.spec.ts'
```

Expected: exit 0; successful companies remain committed when another company fails; every result exposes snapshot/master/rule lineage; no LLM, prompt, vector, or Agent dependency exists.

- [ ] **Step 4: Validate and commit Phase 1 evidence**

Run: `python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-1/formula-command.json artifacts/acceptance/phase-1/formula-report.xml artifacts/acceptance/phase-1/full-stack-command.json artifacts/acceptance/phase-1/full-stack.xml`

Expected: exit 0 with no missing file or hash mismatch.

```bash
git add artifacts/acceptance/phase-1
git commit -m "test(acceptance): approve deterministic tax foundation"
```

### Task 3: Implement and accept Phase 2 business-entertainment monitoring

**Files:**
- Execute: `docs/superpowers/plans/2026-07-12-phase-2-business-entertainment-agent.md`
- Generate: `artifacts/acceptance/phase-2/backend.xml`
- Generate: `artifacts/acceptance/phase-2/backend-command.json`
- Generate: `artifacts/acceptance/phase-2/web-command.json`

- [ ] **Step 1: Execute every Phase 2 task and commit checkpoint**

Block completion unless all five source datasets use IngestBatch/snapshots; the effective company list is enforced; SAP-linked and unlinked-business-document paths remain separate; standalone SAP only enters coverage; exact late linkage is server-revalidated and merged without duplicate exposure.

- [ ] **Step 2: Record backend, security, gold-set, and E2E evidence**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-2-backend \
  --output artifacts/acceptance/phase-2/backend-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/business_entertainment tests/unit/semantic tests/integration/application tests/integration/api/test_business_entertainment_api.py tests/integration/api/test_entertainment_export.py tests/integration/workers/test_business_entertainment_worker.py tests/security tests/evaluation/test_golden_governance.py tests/evaluation/test_business_entertainment_metrics.py tests/e2e/test_business_entertainment_pipeline.py -q --junitxml=../artifacts/acceptance/phase-2/backend.xml'
```

Expected: exit 0; known typical cases have zero misses, pilot recall is at least 90%, formal gate recall is at least 95%, high-confidence accuracy is at least 80%, and all linkage/merge/KPI invariants pass.

- [ ] **Step 3: Record frontend and browser evidence**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-2-web \
  --output artifacts/acceptance/phase-2/web-command.json \
  -- bash -lc 'cd web && npm test -- --run && npm run build && npx playwright test e2e/business-entertainment.spec.ts'
```

Expected: exit 0; UI displays `SAP凭证待定位`, exact evidence, account suggestions, coverage semantics, merge history, and one active total.

- [ ] **Step 4: Validate and commit Phase 2 evidence**

Run: `python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-2/backend-command.json artifacts/acceptance/phase-2/backend.xml artifacts/acceptance/phase-2/web-command.json`

Expected: exit 0.

```bash
git add artifacts/acceptance/phase-2
git commit -m "test(acceptance): approve entertainment evidence paths"
```

### Task 4: Implement and accept Phase 3 welfare and donation monitoring

**Files:**
- Execute: `docs/superpowers/plans/2026-07-12-phase-3-welfare-donation-agents.md`
- Generate: `artifacts/acceptance/phase-3/backend.xml`
- Generate: `artifacts/acceptance/phase-3/backend-command.json`
- Generate: `artifacts/acceptance/phase-3/web-command.json`

- [ ] **Step 1: Execute every Phase 3 task and commit checkpoint**

Block completion unless the exact 14%/12% greater-than-zero gates, missing-input behavior, complete YTD SAP lines, shared Phase 2 semantic contracts, evidence validation, transaction routing, worker isolation, and rerun idempotency pass.

- [ ] **Step 2: Record backend scope, semantic, worker, and E2E evidence**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-3-backend \
  --output artifacts/acceptance/phase-3/backend-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/semantic tests/unit/workers/test_monthly_semantic_batch.py tests/integration/application/test_monthly_semantic_ingest_snapshot.py tests/integration/application/test_sap_voucher_monitor_transaction.py tests/integration/persistence/test_monthly_semantic_repository.py tests/integration/cases/test_welfare_donation_cases.py tests/integration/api/test_monthly_semantic_routes.py tests/integration/workers/test_monthly_semantic_batch_eager.py tests/evaluation/test_welfare_donation_golden.py tests/e2e/test_phase_3_monthly_semantic_flow.py -q --junitxml=../artifacts/acceptance/phase-3/backend.xml'
```

Expected: exit 0; scope formulas are 100% correct, known cases have zero misses, pilot recall is at least 90%, formal gate recall is at least 95%, and high-confidence accuracy is at least 80%.

- [ ] **Step 3: Record frontend and browser evidence**

Run:

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-3-web \
  --output artifacts/acceptance/phase-3/web-command.json \
  -- bash -lc 'cd web && npm test -- --run && npm run build && npx playwright test e2e/phase-3-welfare-donation.spec.ts'
```

Expected: exit 0; welfare and donation filters, SAP evidence, candidate account, confidence, versions, and human actions are visible and scoped.

- [ ] **Step 4: Validate and commit Phase 3 evidence**

Run: `python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-3/backend-command.json artifacts/acceptance/phase-3/backend.xml artifacts/acceptance/phase-3/web-command.json`

Expected: exit 0.

```bash
git add artifacts/acceptance/phase-3
git commit -m "test(acceptance): approve welfare and donation monitors"
```

### Task 5: Implement and accept Phase 4 production readiness

**Files:**
- Execute: `docs/superpowers/plans/2026-07-12-phase-4-governance-hardening-rollout.md`
- Verify: `Makefile`
- Verify: `artifacts/acceptance/phase-4/governance.xml`
- Verify: `artifacts/acceptance/phase-4/replay-report.json`
- Verify: `artifacts/acceptance/phase-4/capacity-report.json`
- Verify: `artifacts/acceptance/phase-4/rollback-report.json`
- Verify: `artifacts/acceptance/phase-4/uat-scorecard.json`

- [ ] **Step 1: Execute every Phase 4 task and assert targets exist**

Run: `test -f Makefile && make -n verify-governance verify-release verify-capacity verify-rollback verify-migrations security-check uat`

Expected: exit 0 and every target expands to a checked-in script; no undefined target is mistaken for a business-gate failure.

- [ ] **Step 2: Run governance, release, capacity, migration, security, rollback, and UAT gates**

Run:

```bash
make verify-governance
make verify-release
make verify-capacity COMPANY_FIXTURE=126
make verify-migrations
make security-check
make verify-rollback
make uat SNAPSHOT_SET=pilot-2026q2
```

Expected: API/RLS/semantic-evidence isolation and no-external-index assertion pass; signed manifest and replay verify; valid-company success is at least 98%; the reference profile completes within 24 hours; valid-company monthly output is ready within 48 hours; rollback is repeatable/resumable; `production_ready=true` only with all approvals.

- [ ] **Step 3: Validate every Phase 4 artifact and the complete migration registry**

Run:

```bash
python3 scripts/acceptance/verify_artifact.py \
  artifacts/acceptance/phase-4/governance.xml \
  artifacts/acceptance/phase-4/replay-report.json \
  artifacts/acceptance/phase-4/capacity-report.json \
  artifacts/acceptance/phase-4/rollback-report.json \
  artifacts/acceptance/phase-4/uat-scorecard.json
python3 scripts/acceptance/verify_contract_registry.py \
  --plans-dir docs/superpowers/plans \
  --migrations-dir backend/migrations/versions \
  --mode plans-and-code \
  --output artifacts/acceptance/contract-registry-final.json
```

Expected: exit 0; the actual Alembic chain exactly matches the registry through `0007`.

- [ ] **Step 4: Commit Phase 4 and final registry evidence**

```bash
git add artifacts/acceptance/phase-4 artifacts/acceptance/contract-registry-final.json
git commit -m "test(acceptance): approve production readiness"
```

### Task 6: Sign the complete evidence set and enforce the rollout/rollback order

**Files:**
- Modify: `docs/operations/acceptance-scorecard.md`
- Verify: `infra/runbooks/group-rollout.md`
- Verify: `infra/runbooks/rollback.md`
- Generate: `artifacts/acceptance/final-evidence-manifest.json`
- Generate: `artifacts/acceptance/final-evidence-manifest.sig`

Cross-phase invariants:

| Contract | Required invariant |
|---|---|
| Ingestion and lineage | Every result identifies source, period, batch, row, validation, and immutable snapshot. |
| Tax master data | Rate, loss carryforward, and prior-three-full-year burden are matched by company, never inferred. |
| Money/rate | Decimal intermediates, approved final `ROUND_HALF_UP`, and display-independent ratio threshold. |
| Principal and scope | One identity model; API, RLS, and PostgreSQL semantic evidence use the same company scope. |
| Risk case | Stable fingerprint, explicit lifecycle, idempotent reruns, compatible extensions, and traceable history. |
| Semantic decision | Provider-neutral model judgment, validated citations/accounts, versioned artifacts, and human final decision. |
| Business-document identity | Exact links only; no arbitrary attachment; late SAP merge has one active exposure. |
| Signed release | Application and all rule/model/evidence artifacts are attributable, replayed, verified, and reversible. |

- [ ] **Step 1: Build and verify the final signed evidence manifest**

Run: `make verify-release EVIDENCE_ROOT=artifacts/acceptance FINAL_MANIFEST=artifacts/acceptance/final-evidence-manifest.json`

Expected: exit 0; manifest and signature cover Phase 1–4 command results, JUnit/results, migration registry, scorecard, application image, rules, prompts, model configuration, account dictionary, and case library.

- [ ] **Step 2: Execute the single repeatable rollback command before promotion**

Run: `make verify-rollback CANDIDATE_MANIFEST=artifacts/acceptance/final-evidence-manifest.json`

Expected state sequence: `PREFLIGHT_VERIFIED → TASKS_DRAINED_OR_REVOKED → EXPORTS_REVOKED → RESTORE_VERIFIED → PREVIOUS_RELEASE_DEPLOYED → CHECKSUMS_MATCHED → REPRESENTATIVE_RERUN_PASSED → RECOVERY_VERIFIED`. Repeating the command with the same approved inputs skips verified stages and returns the same recovery result.

- [ ] **Step 3: Apply the rollout order and stop on any missing evidence**

Deploy backward-compatible migrations/adapters, run shadow calculation, compare with approved workbooks/cases, enable quarterly risks for pilots, then enable one semantic monitor and cohort per wave. Never downgrade the production database until the same downgrade succeeds on a restored copy. Preserve all risk and audit history.

- [ ] **Step 4: Record approvals and commit the final handoff**

Finance, tax, data owners, security, and operations sign the scorecard with evidence hashes. Missing signature, artifact, threshold, or rollback checkpoint blocks promotion.

```bash
git add docs/operations/acceptance-scorecard.md artifacts/acceptance/final-evidence-manifest.json artifacts/acceptance/final-evidence-manifest.sig
git commit -m "docs: approve phased tax monitoring rollout"
```

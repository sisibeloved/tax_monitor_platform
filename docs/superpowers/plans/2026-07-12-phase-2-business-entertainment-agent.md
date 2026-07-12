# Phase 2 Business Entertainment Agent Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段1确定性底座上交付业务招待费专业Agent，完整接入SAP、合思和三类OA数据，构建可审计的精确证据链，同时允许未关联OA/合思业务单据形成“SAP凭证待定位”的正式风险，且不得任意归因或重复统计风险。

**Architecture:** 所有来源复用阶段1 `IngestBatch → SourceRecord → AccountingSnapshot → SnapshotSet` 不可变链路。确定性服务先生成 `SAP_LINKED`、`BUSINESS_DOCUMENT_UNLINKED` 和 `SapLinkCoverage`；高召回候选再经厂商中立 `StructuredModelClient`、严格 `SemanticModelJudgment` schema和独立证据复核，最后由服务端组装权威 `SemanticDetection`。案件、驾驶舱、导出和KPI只聚合未合并根案件。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2/Alembic、PostgreSQL、Celery/Redis、pytest/Hypothesis、httpx、openpyxl；React、TypeScript、Vite、Ant Design、TanStack Query、Vitest/Testing Library、Playwright。

---

## 0. Execution Contract

### 0.1 Authoritative references

- V0.8 specification: `docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md`.
- V0.8 detailed design: `docs/design/detailed/2026-07-12-group-income-tax-risk-monitoring-platform-detailed.md`.
- Phase 1 plan: `docs/superpowers/plans/2026-07-12-phase-1-foundation-quarterly.md`.
- Phase 1 is authoritative for existing paths, API prefix, pytest invocation, Principal scope, IngestBatch, snapshot, case, persistence and Celery contracts.

Before Task 1:

~~~bash
cd backend
pytest -q
cd ..
npm --prefix web test -- --run
~~~

Expected: all phase1 backend and web tests PASS. If phase1 is not green, stop; do not create a parallel scaffold.

### 0.2 Scope and exclusions

In scope:

- Versioned business-entertainment company list with upload, review, effective period and blocking quality gate.
- SAP expense vouchers plus four predecessor sources: OA business-entertainment application, OA self-procurement reimbursement, OA material requisition and Hesi business-entertainment reimbursement.
- Phase1 IngestBatch adapters, source lineage, immutable snapshots and SnapshotSet reads for every source.
- Exact SAP and OA/Hesi linking, two semantic evaluation modes, standalone-SAP coverage, high-recall candidate lexicon.
- Shared semantic SAP-voucher observation, provider-neutral structured-model port, enterprise model adapter and artifact version governance.
- Professional judgment, evidence validation, suggested-account governance, risk cases, human workflow, exact-link resolution, dashboard, Excel export, KPI and UI.
- Dual-annotator golden set, prompt-injection/PII/zero-retention tests and backend/frontend E2E.

Out of scope:

- Welfare expense and public-welfare donation behavior.
- Fuzzy-link automatic attachment, automatic accounting entries, online self-learning and public-model training.
- Agent judgment for a SAP voucher with no exact predecessor link; it belongs only to `SapLinkCoverage`.
- Replacing phase1 source, snapshot, Principal, case, database session, Celery or quarterly modules.

### 0.3 Locked business invariants

1. Automatic SAP linkage requires direct voucher/line reference or exact document ID in SAP assignment/reference; amount/date/person similarity is FUZZY only.
2. Hesi linked exactly to OA is canonical; OA and its self-procurement/material documents are evidence. Unlinked self-procurement/material documents do not independently create risk.
3. `SAP_LINKED` requires SAP identity and uses SAP amount. `BUSINESS_DOCUMENT_UNLINKED` uses Hesi or OA canonical identity, permits null SAP fields and uses canonical-document amount.
4. A SAP voucher without an exact predecessor link creates one coverage observation and no semantic evaluation.
5. Only suspected-misposting labels create RiskCase. `CURRENT_ACCOUNT_REASONABLE` stores DetectionRecord only; `INSUFFICIENT_EVIDENCE` creates EvidenceTask.
6. Model output never owns company, period, source mode, canonical identity, SAP references, amount, snapshot or version fields.
7. Resolve requests submit a persisted evidence-link ID only. The server reloads and revalidates EXACT quality, company, source, target and snapshot lineage in the merge transaction.
8. Merged source cases remain for audit but lists, dashboard, exports and KPI count only root cases where `merged_into_case_id IS NULL`.
9. `SapExpenseVoucherObservation` is immutable source normalization bound only to `SourceRecord`. A separate `SapExpenseVoucherSnapshotProjection` with a non-null snapshot FK is inserted inside the SnapshotSet publication transaction; neither entity is updated later to attach a snapshot. Loaders return a frozen `SnapshotBoundSapExpenseVoucher` DTO that combines both IDs without mutating either record.

### 0.4 Canonical file map

Shared contracts created in phase2 and reused by phase3:

- `backend/src/tax_risk/domain/semantic/sap_voucher.py` — source observation, snapshot projection, frozen bound-view DTO and account-family enum.
- `backend/src/tax_risk/domain/semantic/contracts.py` — `SemanticModelJudgment` and server-owned `SemanticDetection`.
- `backend/src/tax_risk/domain/semantic/account_dictionary.py` — one immutable versioned suggested-account dictionary.
- `backend/src/tax_risk/application/semantic/model_client.py` — `StructuredModelClient` Protocol.
- `backend/src/tax_risk/application/semantic/evidence_review.py` — shared SAP-voucher EvidencePack builder and citation resolver.
- `backend/src/tax_risk/application/semantic/detection_router.py` — one-transaction SAP detection/EvidenceTask/RiskCase routing.
- `backend/src/tax_risk/application/semantic/version_registry.py` — model, prompt and case-library approval/publication.
- `backend/src/tax_risk/persistence/semantic_models.py` — shared SAP observation, artifact-version, account-dictionary and model-call-audit ORM.
- `backend/src/tax_risk/persistence/semantic_repositories.py` — focused shared repositories.

Business-entertainment-specific files:

- `backend/src/tax_risk/domain/business_entertainment/source_models.py`
- `backend/src/tax_risk/domain/business_entertainment/company_scope.py`
- `backend/src/tax_risk/domain/business_entertainment/evaluation.py`
- `backend/src/tax_risk/domain/business_entertainment/lexicon.py`
- `backend/src/tax_risk/rules/business_entertainment_candidate_lexicon.v1.yaml`
- `backend/src/tax_risk/adapters/ingest/sap_business_entertainment_csv.py`
- `backend/src/tax_risk/adapters/ingest/hesi_business_entertainment_csv.py`
- `backend/src/tax_risk/adapters/ingest/oa_business_entertainment_csv.py`
- `backend/src/tax_risk/adapters/ingest/oa_self_procurement_csv.py`
- `backend/src/tax_risk/adapters/ingest/oa_material_requisition_csv.py`
- `backend/src/tax_risk/adapters/ingest/business_entertainment_company_list_xlsx.py`
- `backend/src/tax_risk/application/business_entertainment/source_loader.py`
- `backend/src/tax_risk/application/business_entertainment/company_scope.py`
- `backend/src/tax_risk/application/business_entertainment/linker.py`
- `backend/src/tax_risk/application/business_entertainment/evaluation_items.py`
- `backend/src/tax_risk/application/business_entertainment/candidates.py`
- `backend/src/tax_risk/application/business_entertainment/agent.py`
- `backend/src/tax_risk/application/business_entertainment/evidence_review.py`
- `backend/src/tax_risk/application/business_entertainment/service.py`
- `backend/src/tax_risk/application/business_entertainment/reporting.py`
- `backend/src/tax_risk/application/business_entertainment/export.py`
- `backend/src/tax_risk/application/cases.py`
- `backend/src/tax_risk/application/case_merge.py`
- `backend/src/tax_risk/persistence/business_entertainment_models.py`
- `backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- `backend/src/tax_risk/adapters/model/enterprise_structured_client.py`
- `backend/src/tax_risk/adapters/model/fake_structured_client.py`
- `backend/src/tax_risk/workers/business_entertainment.py`
- `backend/src/tax_risk/api/routes/business_entertainment.py`
- `backend/src/tax_risk/api/routes/semantic_governance.py`
- `backend/src/tax_risk/api/routes/exports.py`
- `web/src/features/risks/{api.ts,types.ts,RiskListPage.tsx,RiskDetailPage.tsx}`
- `web/src/features/business-entertainment/{api.ts,types.ts,SapLinkCoveragePage.tsx}`

Existing phase1 files modified, never duplicated:

- `backend/pyproject.toml`
- `backend/src/tax_risk/config.py`
- `backend/src/tax_risk/domain/cases.py`
- `backend/src/tax_risk/persistence/models.py` only to expose shared Base/metadata imports; no phase2 table bodies.
- `backend/src/tax_risk/persistence/repositories.py` only to reuse phase1 session/unit-of-work helpers.
- `backend/src/tax_risk/application/ingest.py`
- `backend/src/tax_risk/application/snapshots.py`
- `backend/src/tax_risk/api/schemas.py`
- `backend/src/tax_risk/api/routes/cases.py`
- `backend/src/tax_risk/api/routes/dashboard.py`
- `backend/src/tax_risk/main.py`
- `backend/src/tax_risk/workers/celery_app.py`
- `backend/migrations/env.py`
- `web/src/App.tsx`

Migration chain:

- `0002a_business_entertainment_scope.py`: down_revision = phase1 `0001_control_plane`.
- `0002b_business_entertainment_observations.py`: down_revision = `0002a_business_entertainment_scope`.
- `0002c_semantic_contracts_accounts.py`: down_revision = `0002b_business_entertainment_observations`.
- `0002d_semantic_artifacts_calls.py`: down_revision = `0002c_semantic_contracts_accounts`.
- Phase3 `0003_welfare_donation_agents.py` must set down_revision to `0002d_semantic_artifacts_calls`.

## Chunk 1: Controlled Sources, Exact Evidence, and High-Recall Candidates

### Task 1: Add the versioned business-entertainment company list and blocking quality gate

**Files:**

- Create: `backend/src/tax_risk/domain/business_entertainment/company_scope.py`
- Create: `backend/src/tax_risk/adapters/ingest/business_entertainment_company_list_xlsx.py`
- Create: `backend/src/tax_risk/application/business_entertainment/company_scope.py`
- Modify: `backend/src/tax_risk/application/ingest.py`
- Create: `backend/src/tax_risk/persistence/business_entertainment_models.py`
- Create: `backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- Create: `backend/migrations/versions/0002a_business_entertainment_scope.py`
- Modify: `backend/migrations/env.py`
- Test: `backend/tests/unit/adapters/test_business_entertainment_company_list_xlsx.py`
- Test: `backend/tests/integration/application/test_business_entertainment_company_scope.py`

- [ ] **Step 1: Write RED import and effective-version tests**

Test required columns `company_code, effective_from, effective_to`; reject blank/unknown/duplicate companies, invalid ranges and overlapping published versions. Assert one reviewer cannot be the uploader and reviewer.

- [ ] **Step 2: Run RED tests**

Run: `cd backend && pytest tests/unit/adapters/test_business_entertainment_company_list_xlsx.py tests/integration/application/test_business_entertainment_company_scope.py -q`

Expected: FAIL because importer, models and service do not exist.

- [ ] **Step 3: Implement immutable scope contracts**

Define:

~~~python
class ScopeVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"

class BusinessEntertainmentScopeVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version_id: UUID
    effective_from: date
    effective_to: date
    source_file_name: str
    file_checksum: str
    uploader_id: str
    reviewer_id: str | None
    status: ScopeVersionStatus
~~~

Persist version header and company rows with unique `version_id + company_code`, non-overlapping published effective periods and foreign-key validation against phase1 Company.

- [ ] **Step 4: Implement upload, review, publish and quality gate**

The XLSX adapter first creates a phase1 IngestBatch/SourceRecord lineage, then the gate returns exactly one published effective version for the requested month. Missing, duplicate, overlapping or unapproved versions create a DataIssue and block only the business-entertainment monitor; no company is silently inferred.

- [ ] **Step 5: Run GREEN tests**

Run: `cd backend && alembic upgrade head && pytest tests/unit/adapters/test_business_entertainment_company_list_xlsx.py tests/integration/application/test_business_entertainment_company_scope.py -q`

Expected: PASS; effective scope is deterministic and rejected versions cannot run.

- [ ] **Step 6: Commit Task 1**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/company_scope.py backend/src/tax_risk/adapters/ingest/business_entertainment_company_list_xlsx.py backend/src/tax_risk/application/business_entertainment/company_scope.py backend/src/tax_risk/application/ingest.py backend/src/tax_risk/persistence/business_entertainment_models.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/migrations/versions/0002a_business_entertainment_scope.py backend/migrations/env.py backend/tests
git commit -m "feat: govern entertainment company scope"
~~~

### Task 2: Define and ingest all five source datasets through phase1 IngestBatch

**Files:**

- Create: `backend/src/tax_risk/domain/semantic/sap_voucher.py`
- Create: `backend/src/tax_risk/domain/business_entertainment/source_models.py`
- Create: five CSV adapters listed in Section 0.4
- Modify: `backend/src/tax_risk/application/ingest.py`
- Create: `backend/src/tax_risk/persistence/semantic_models.py`
- Create: `backend/src/tax_risk/persistence/semantic_repositories.py`
- Modify: `backend/src/tax_risk/persistence/business_entertainment_models.py`
- Create: `backend/migrations/versions/0002b_business_entertainment_observations.py`
- Modify: `backend/migrations/env.py`
- Test: `backend/tests/unit/adapters/test_business_entertainment_source_adapters.py`
- Test: `backend/tests/integration/application/test_business_entertainment_ingest.py`

- [ ] **Step 1: Write RED schema tests for every source**

Lock these fields and constraints:

- SAP: company, fiscal year, period, posting date, voucher, line, current account, Decimal amount, currency, summary, assignment/reference, reversal reference, account family; key is company+year+voucher+line.
- Hesi: company, year, period, reimbursement ID, line ID, date, Decimal amount, currency, summary, reimbursement purpose, reception-target category, participant count, linked OA ID, optional direct SAP voucher/line; key is company+reimbursement+line.
- OA entertainment application: company, application ID, line ID, date, purpose, reception-target category, participant count, Decimal requested amount/currency; key is company+application+line.
- OA self-procurement: company, request ID, line ID, date, item, purpose, recipient category, Decimal amount/currency, exact parent OA/Hesi ID; key is company+request+line.
- OA material requisition: company, requisition ID, line ID, date, material, use, recipient category, quantity/unit, optional Decimal amount/currency, exact parent OA/Hesi ID; key is company+requisition+line.

All Pydantic models use `extra="forbid"`. Identity and period fields are nonblank; SAP reversal amounts may be negative; participant names and phone/identity numbers are not accepted into normalized semantic schemas.

- [ ] **Step 2: Run schema tests and confirm RED**

Run: `cd backend && pytest tests/unit/adapters/test_business_entertainment_source_adapters.py -q`

Expected: collection FAIL because contracts/adapters are missing.

- [ ] **Step 3: Implement adapters against phase1 AdapterResult**

Each adapter emits source schema version, source primary-key definition, accepted/rejected counts, Decimal control total and row errors. Register dataset types in phase1 `application/ingest.py`; never bypass IngestBatch or write analysis tables directly.

- [ ] **Step 4: Run schema tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/adapters/test_business_entertainment_source_adapters.py -q`

Expected: PASS for valid, duplicate, malformed and PII-rejection fixtures.

- [ ] **Step 5: Write RED lineage integration tests**

Assert every normalized observation references `ingest_batch_id` and `source_record_id`; PARTIAL batch remains not ready; duplicate source keys are rejected; each source control total reconciles.

- [ ] **Step 6: Run lineage tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_business_entertainment_ingest.py -q`

Expected: FAIL before repository wiring.

- [ ] **Step 7: Persist focused source indexes**

`SapExpenseVoucherObservation` lives in `semantic_models.py` with UUID, non-null source_record FK, source key, created_at, company/year/period, voucher/line/account, Decimal amount/currency, reversal reference and `account_family=BUSINESS_ENTERTAINMENT`; it has no snapshot FK. `SapExpenseVoucherSnapshotProjection(id, observation_id, snapshot_id, company_code, period, created_at)` has non-null FKs, is unique by snapshot+observation and is immutable after insert. The domain defines frozen `SnapshotBoundSapExpenseVoucher` with observation fields plus projection ID, snapshot ID and source-record ID; it is a read DTO, not another table. OA/Hesi source indexes likewise bind only to SourceRecord. The same migration defines: `evidence_link(id, company_code, source_record_id, target_record_id, relation_kind, relation_quality, matched_field, snapshot_id, created_at)` unique by snapshot+source+target+kind; `business_entertainment_evaluation(id, candidate_key, company_code, fiscal_year, period, source_mode, canonical_record_type, canonical_source_record_id, sap_observation_id nullable, amount, amount_source, snapshot_id, created_at)` unique by snapshot+candidate key; and `sap_link_coverage(id, company_code, period, sap_observation_id, link_status, exact_evidence_link_id nullable, evaluated_via_business_document, snapshot_id, created_at)` unique by snapshot+SAP observation. No raw duplicate payload is stored.

- [ ] **Step 8: Run lineage tests and confirm GREEN**

Run: `cd backend && alembic upgrade head && pytest tests/integration/application/test_business_entertainment_ingest.py -q`

Expected: PASS; all five datasets trace to IngestBatch/SourceRecord.

- [ ] **Step 9: Commit Task 2**

~~~bash
git add backend/src/tax_risk/domain backend/src/tax_risk/adapters/ingest backend/src/tax_risk/application/ingest.py backend/src/tax_risk/persistence backend/tests/unit/adapters/test_business_entertainment_source_adapters.py backend/tests/integration/application/test_business_entertainment_ingest.py
git commit -m "feat: ingest entertainment evidence with lineage"
~~~

### Task 3: Load immutable snapshots and build exact links without arbitrary attribution

**Files:**

- Create: `backend/src/tax_risk/application/business_entertainment/source_loader.py`
- Create: `backend/src/tax_risk/application/business_entertainment/linker.py`
- Modify: `backend/src/tax_risk/application/snapshots.py`
- Modify: `backend/src/tax_risk/persistence/semantic_models.py`
- Modify: `backend/src/tax_risk/persistence/semantic_repositories.py`
- Modify: `backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- Test: `backend/tests/unit/business_entertainment/test_exact_linker.py`
- Test: `backend/tests/integration/application/test_entertainment_snapshot_loader.py`

- [ ] **Step 1: Write RED immutable-loader tests**

Require one `PUBLISHED` SnapshotSet containing all required company/period source members and a non-null UTC `published_at`. In the same complete-quality-gate publication transaction, insert all `SapExpenseVoucherSnapshotProjection` rows with non-null snapshot IDs, transition the SnapshotSet to `PUBLISHED`, write `published_at` exactly once and make the set/members/projections immutable. DRAFT/VALIDATED, missing or mutable members return DataIssue and no evaluation input. Reads for January through target month must be bounded by the PUBLISHED SnapshotSet, not current source tables. Tests must reject pre-publication reads, null snapshot projections, post-publication UPDATE/DELETE and any later attempt to attach an observation to a snapshot.

- [ ] **Step 2: Run loader tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_entertainment_snapshot_loader.py -q`

Expected: FAIL because the loader is absent.

- [ ] **Step 3: Implement SnapshotSet-only source loading**

Reuse phase1 snapshot membership and company scope. During the Phase1 publication transaction, resolve member SourceRecords to immutable observations, insert snapshot-specific projections, transition the complete set to `PUBLISHED` and write UTC `published_at` before one commit. Freeze repository signature `load_snapshot_bound_sap_vouchers(snapshot_set_id, account_family, company_code, period_end) -> list[SnapshotBoundSapExpenseVoucher]`; it rejects every non-PUBLISHED set and reads only projections belonging to the requested PUBLISHED set. Do not infer missing data as zero or empty-safe and never mutate observations to add a snapshot.

- [ ] **Step 4: Run loader tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/application/test_entertainment_snapshot_loader.py -q`

Expected: PASS; DRAFT/VALIDATED sets cannot load, and changing later source batches does not alter the loaded PUBLISHED set.

- [ ] **Step 5: Write RED exact-link tests**

Cover direct SAP voucher/line, exact SAP assignment/reference document ID, Hesi→OA canonical priority, self-procurement/material exact-parent evidence, cross-company rejection, ambiguous duplicate refs and amount/date/person-only FUZZY hints.

- [ ] **Step 6: Run linker tests and confirm RED**

Run: `cd backend && pytest tests/unit/business_entertainment/test_exact_linker.py -q`

Expected: FAIL because the linker is absent.

- [ ] **Step 7: Implement deterministic linker**

Return exact links, fuzzy hints, conflicts, unlinked SAP keys and unlinked canonical business keys. Exact links persist source/target record IDs, relation kind, relation quality, matching field, snapshot ID and created_at. FUZZY links never become evidence for risk.

- [ ] **Step 8: Run linker tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/business_entertainment/test_exact_linker.py -q`

Expected: PASS, including Hypothesis input-order invariance.

- [ ] **Step 9: Commit Task 3**

~~~bash
git add backend/src/tax_risk/application/business_entertainment/source_loader.py backend/src/tax_risk/application/business_entertainment/linker.py backend/src/tax_risk/application/snapshots.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/tests
git commit -m "feat: link immutable entertainment evidence"
~~~

### Task 4: Build two evaluation modes and standalone-SAP coverage

**Files:**

- Create: `backend/src/tax_risk/domain/business_entertainment/evaluation.py`
- Create: `backend/src/tax_risk/application/business_entertainment/evaluation_items.py`
- Modify: `backend/src/tax_risk/persistence/business_entertainment_models.py`
- Modify: `backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- Test: `backend/tests/unit/business_entertainment/test_evaluation_items.py`
- Test: `backend/tests/integration/persistence/test_sap_link_coverage.py`

- [ ] **Step 1: Write RED evaluation tests**

Assert exact SAP chains become one `SAP_LINKED` item; exact Hesi/OA without SAP becomes one Hesi-canonical `BUSINESS_DOCUMENT_UNLINKED` item; standalone OA/Hesi becomes unlinked; self-procurement/material alone does not become canonical; standalone SAP creates coverage only.

- [ ] **Step 2: Run evaluation tests and confirm RED**

Run: `cd backend && pytest tests/unit/business_entertainment/test_evaluation_items.py -q`

Expected: FAIL because contracts/builder are absent.

- [ ] **Step 3: Implement immutable evaluation contracts and builder**

Include candidate key, company/year/period, source mode, canonical type/key, nullable SAP key/voucher/line/account, Decimal amount, amount source, exact evidence IDs and snapshot ID. Enforce SAP fields present only for linked mode.

- [ ] **Step 4: Run evaluation tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/business_entertainment/test_evaluation_items.py -q`

Expected: PASS.

- [ ] **Step 5: Write RED coverage persistence tests**

Assert every SAP entertainment observation creates exactly one LINKED/UNLINKED coverage row per snapshot, unlinked rows set `evaluated_via_business_document=false`, and rerun is idempotent.

- [ ] **Step 6: Run coverage tests and confirm RED**

Run: `cd backend && pytest tests/integration/persistence/test_sap_link_coverage.py -q`

Expected: FAIL before coverage ORM/repository implementation.

- [ ] **Step 7: Implement coverage persistence**

Store company, period, SAP observation ID, voucher/line, Decimal amount, link status, exact evidence-link ID if present, evaluated flag, snapshot ID and created_at with a unique snapshot+SAP-observation constraint.

- [ ] **Step 8: Run coverage tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/persistence/test_sap_link_coverage.py -q`

Expected: PASS and repeated writes do not change counts.

- [ ] **Step 9: Commit Task 4**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/evaluation.py backend/src/tax_risk/application/business_entertainment/evaluation_items.py backend/src/tax_risk/persistence/business_entertainment_models.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/tests/unit/business_entertainment/test_evaluation_items.py backend/tests/integration/persistence/test_sap_link_coverage.py
git commit -m "feat: build entertainment evaluation modes"
~~~

### Task 5: Add a versioned high-recall lexicon and idempotent monthly worker

**Files:**

- Create: `backend/src/tax_risk/domain/business_entertainment/lexicon.py`
- Create: `backend/src/tax_risk/rules/business_entertainment_candidate_lexicon.v1.yaml`
- Create: `backend/src/tax_risk/application/business_entertainment/candidates.py`
- Create: `backend/src/tax_risk/application/business_entertainment/service.py`
- Create: `backend/src/tax_risk/workers/business_entertainment.py`
- Modify: `backend/src/tax_risk/workers/celery_app.py`
- Test: `backend/tests/unit/business_entertainment/test_candidate_lexicon.py`
- Test: `backend/tests/integration/workers/test_business_entertainment_worker.py`

- [ ] **Step 1: Write RED lexicon schema and recall tests**

Each YAML version contains `version, monitor_type, effective_from, status` and entries with `signal_id, canonical_phrase, aliases, allowed_fields, priority, label_hints`. Unknown keys, duplicate IDs and empty aliases fail. Include all V0.8 signals; no negative term may suppress a positive hit.

- [ ] **Step 2: Run lexicon tests and confirm RED**

Run: `cd backend && pytest tests/unit/business_entertainment/test_candidate_lexicon.py -q`

Expected: FAIL because schema/file/generator are absent.

- [ ] **Step 3: Implement version loader and candidate union**

Normalize punctuation and whitespace, preserve cited field/spans, union all hits and retain a low-priority full-scan evaluation lane. Candidate output is a screening record, never a final accounting conclusion.

- [ ] **Step 4: Run lexicon tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/business_entertainment/test_candidate_lexicon.py -q`

Expected: PASS; every known positive creates at least one candidate.

- [ ] **Step 5: Write RED worker tests**

Verify effective company list, January-to-month PUBLISHED SnapshotSet with immutable `published_at`, per-company isolation, stable idempotency key, coverage-before-Agent order and exclusion of standalone SAP rows from Agent calls.

- [ ] **Step 6: Run worker tests and confirm RED**

Run: `cd backend && pytest tests/integration/workers/test_business_entertainment_worker.py -q`

Expected: FAIL because worker registration is absent.

- [ ] **Step 7: Implement thin Celery orchestration**

Reuse phase1 batch status, retry taxonomy and company isolation. Return counts for scope, sources, exact/fuzzy/conflict links, both evaluation modes, standalone SAP coverage, candidates, detections, evidence tasks and risks.

- [ ] **Step 8: Run worker tests and Chunk 1 regression**

Run:

~~~bash
cd backend
pytest tests/unit/business_entertainment tests/unit/adapters tests/integration/application tests/integration/persistence tests/integration/workers/test_business_entertainment_worker.py -q
~~~

Expected: PASS; one failed company does not stop other companies and rerun does not duplicate rows.

- [ ] **Step 9: Commit Task 5**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/lexicon.py backend/src/tax_risk/rules backend/src/tax_risk/application/business_entertainment backend/src/tax_risk/workers backend/tests
git commit -m "feat: generate versioned entertainment candidates"
~~~

## Chunk 2: Governed Structured Agent, Human Cases, Reporting, and Release Gates

### Task 6: Split model judgment from server detection and govern suggested accounts

**Files:**

- Create: `backend/src/tax_risk/domain/semantic/contracts.py`
- Create: `backend/src/tax_risk/domain/semantic/account_dictionary.py`
- Create: `backend/src/tax_risk/application/semantic/model_client.py`
- Create: `backend/src/tax_risk/application/semantic/evidence_review.py`
- Create: `backend/src/tax_risk/application/semantic/account_dictionary.py`
- Create: `backend/src/tax_risk/adapters/ingest/suggested_account_dictionary_xlsx.py`
- Modify: `backend/src/tax_risk/application/ingest.py`
- Modify: `backend/src/tax_risk/persistence/semantic_models.py`
- Modify: `backend/src/tax_risk/persistence/semantic_repositories.py`
- Create: `backend/migrations/versions/0002c_semantic_contracts_accounts.py`
- Test: `backend/tests/unit/semantic/test_contract_separation.py`
- Test: `backend/tests/unit/semantic/test_sap_voucher_evidence_pack.py`
- Test: `backend/tests/integration/application/test_account_dictionary_governance.py`

- [ ] **Step 1: Write RED contract-separation tests**

Assert `SemanticModelJudgment` rejects company, SAP, amount, snapshot and version fields. Assert `SemanticDetection` cannot be built without server-owned identity, verified citations and `account_dictionary_version`.

- [ ] **Step 2: Run contract tests and confirm RED**

Run: `cd backend && pytest tests/unit/semantic/test_contract_separation.py -q`

Expected: FAIL because contracts do not exist.

- [ ] **Step 3: Implement the strict split**

~~~python
class SemanticModelJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    semantic_label: SemanticLabel
    confidence_tier: ConfidenceTier
    evidence_citations: list[EvidenceCitation]
    recommended_account_ids: list[str]
    rationale_summary: str
    missing_evidence: list[str]

class StructuredModelClient(Protocol):
    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T:
        raise NotImplementedError
~~~

`SemanticVersionSet` is an immutable server structure containing rule, model, prompt, case-library and account-dictionary version IDs. `SemanticDetection` adds all server-owned candidate/company/period/mode/source/SAP/amount/evidence/version/time fields after validation; it never accepts them from the model response.

- [ ] **Step 4: Run contract tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/semantic/test_contract_separation.py -q`

Expected: PASS; malicious identity fields fail schema validation.

- [ ] **Step 5: Write RED shared SAP-evidence tests**

Assert `build_sap_voucher_evidence_pack(view, versions)` emits only authorized normalized SAP fields, source/snapshot references and stable evidence IDs from one frozen `SnapshotBoundSapExpenseVoucher`. Assert `resolve_citations(judgment, evidence_pack)` rejects foreign IDs, wrong fields and altered quotes.

- [ ] **Step 6: Run shared evidence tests and confirm RED**

Run: `cd backend && pytest tests/unit/semantic/test_sap_voucher_evidence_pack.py -q`

Expected: FAIL because shared builder/resolver are absent.

- [ ] **Step 7: Implement reusable evidence construction**

Freeze signatures `build_sap_voucher_evidence_pack(view: SnapshotBoundSapExpenseVoucher, versions: SemanticVersionSet) -> EvidencePack` and `resolve_citations(judgment: SemanticModelJudgment, evidence_pack: EvidencePack) -> list[EvidenceRef]`. EvidencePack and later SemanticDetection take authoritative snapshot/source IDs from the view. Business entertainment may append exact OA/Hesi evidence after the shared SAP pack; phase3 reuses the SAP-only builder and never adds ORM conversion methods.

- [ ] **Step 8: Run shared evidence tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/semantic/test_sap_voucher_evidence_pack.py -q`

Expected: PASS; foreign or modified citations are rejected.

- [ ] **Step 9: Write RED suggested-account governance tests**

Dictionary rows contain dictionary version, account ID/code/name, accounting category, allowed monitor types/labels, effective period and status. Require uploader/reviewer separation, checksum, approval and immutable publication. Unknown or unpublished IDs must be rejected.

- [ ] **Step 10: Run account tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_account_dictionary_governance.py -q`

Expected: FAIL because import/governance are absent.

- [ ] **Step 11: Implement the one shared authoritative dictionary**

Import XLSX through phase1 IngestBatch/SourceRecord, publish a version only after review, and expose an immutable lookup. Seed phase2 categories for conference expense, employee education, employee welfare and manual review. Phase3 must modify this same file/version model rather than create another dictionary.

- [ ] **Step 12: Run migration and account tests**

Run: `cd backend && alembic upgrade head && pytest tests/integration/application/test_account_dictionary_governance.py -q`

Expected: PASS; only effective published IDs enter detection.

- [ ] **Step 13: Commit Task 6**

~~~bash
git add backend/src/tax_risk/domain/semantic backend/src/tax_risk/application/semantic backend/src/tax_risk/application/ingest.py backend/src/tax_risk/adapters/ingest/suggested_account_dictionary_xlsx.py backend/src/tax_risk/persistence/semantic_models.py backend/src/tax_risk/persistence/semantic_repositories.py backend/migrations/versions/0002c_semantic_contracts_accounts.py backend/tests
git commit -m "feat: govern semantic decisions and accounts"
~~~

### Task 7: Add enterprise model adapter, artifact publication, PII minimization, and call audit

**Files:**

- Create: `backend/src/tax_risk/application/semantic/version_registry.py`
- Create: `backend/src/tax_risk/application/semantic/prompt_safety.py`
- Create: `backend/src/tax_risk/adapters/model/enterprise_structured_client.py`
- Create: `backend/src/tax_risk/adapters/model/fake_structured_client.py`
- Create: `backend/src/tax_risk/api/routes/semantic_governance.py`
- Modify: `backend/src/tax_risk/config.py`
- Modify: `backend/src/tax_risk/persistence/semantic_models.py`
- Modify: `backend/src/tax_risk/persistence/semantic_repositories.py`
- Create: `backend/migrations/versions/0002d_semantic_artifacts_calls.py`
- Modify: `backend/src/tax_risk/api/schemas.py`
- Modify: `backend/src/tax_risk/main.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/unit/adapters/test_enterprise_structured_client.py`
- Test: `backend/tests/integration/application/test_semantic_version_registry.py`
- Test: `backend/tests/security/test_model_pii_retention_audit.py`

- [ ] **Step 1: Write RED artifact-version tests**

Model, prompt and case-library artifacts require type, version, checksum, storage reference/deployment ID, status, uploader, independent reviewer, published time and effective period. Runs reject DRAFT/RETIRED or mismatched versions.

- [ ] **Step 2: Run version tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_semantic_version_registry.py -q`

Expected: FAIL because registry is absent.

- [ ] **Step 3: Implement approval/publication and persistence**

Persist immutable artifact rows and active version set. Publishing uses phase1 Principal and audit context; one active version per artifact type/effective period. SemanticDetection stores model, prompt and case-library versions plus account-dictionary version.

- [ ] **Step 4: Run version tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/application/test_semantic_version_registry.py -q`

Expected: PASS.

- [ ] **Step 5: Write RED enterprise-adapter/security tests**

Require configured enterprise HTTPS endpoint, deployment, timeout, credential reference and `zero_retention_required=true`. Assert phone, identity number and participant names never enter input; source text stays in `input_json`; prompt injection cannot add tools; logs/audit omit raw text.

- [ ] **Step 6: Run adapter/security tests and confirm RED**

Run: `cd backend && alembic upgrade head && pytest tests/unit/adapters/test_enterprise_structured_client.py tests/security/test_model_pii_retention_audit.py -q`

Expected: FAIL because adapter, minimizer and call audit are absent.

- [ ] **Step 7: Implement enterprise and fake adapters**

The enterprise adapter maps the shared Protocol to the controlled endpoint, requests strict JSON schema, disables public training/retention per enterprise contract and has no database tools. The fake adapter is deterministic for tests only and is blocked when environment is production.

- [ ] **Step 8: Persist privacy-safe call audit**

Store call ID, candidate key, company, artifact versions, request/output checksums, allowed-field list, token counts, latency, schema status, retry count, retention-policy confirmation, actor/run IDs and timestamp. Never store full source text, names, phone, identity number, prompt body or model chain-of-thought.

- [ ] **Step 9: Run adapter/security tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/adapters/test_enterprise_structured_client.py tests/security/test_model_pii_retention_audit.py -q`

Expected: PASS; zero-retention confirmation failure blocks the call and creates no risk.

- [ ] **Step 10: Commit Task 7**

~~~bash
git add backend/src/tax_risk/application/semantic backend/src/tax_risk/adapters/model backend/src/tax_risk/api/routes/semantic_governance.py backend/src/tax_risk/api/schemas.py backend/src/tax_risk/main.py backend/src/tax_risk/config.py backend/src/tax_risk/persistence/semantic_models.py backend/src/tax_risk/persistence/semantic_repositories.py backend/migrations/versions/0002d_semantic_artifacts_calls.py backend/pyproject.toml backend/tests
git commit -m "feat: govern enterprise semantic model calls"
~~~

### Task 8: Implement professional judgment, independent evidence review, and case routing

**Files:**

- Create: `backend/src/tax_risk/application/business_entertainment/agent.py`
- Create: `backend/src/tax_risk/application/business_entertainment/evidence_review.py`
- Modify: `backend/src/tax_risk/application/business_entertainment/service.py`
- Create: `backend/src/tax_risk/application/cases.py`
- Create: `backend/src/tax_risk/application/semantic/detection_router.py`
- Modify: `backend/src/tax_risk/domain/cases.py`
- Test: `backend/tests/unit/business_entertainment/test_professional_agent.py`
- Test: `backend/tests/unit/business_entertainment/test_evidence_review.py`
- Test: `backend/tests/unit/semantic/test_detection_router.py`
- Test: `backend/tests/integration/application/test_semantic_case_routing.py`

- [ ] **Step 1: Write RED judgment/review tests**

Cover internal training/meeting meals, employee gathering, valid external reception, conflicting evidence, foreign citations, unsupported accounts, definitive language and source-mode/amount tampering.

- [ ] **Step 2: Run judgment tests and confirm RED**

Run: `cd backend && pytest tests/unit/business_entertainment/test_professional_agent.py tests/unit/business_entertainment/test_evidence_review.py -q`

Expected: FAIL because agent/reviewer are absent.

- [ ] **Step 3: Implement one-item professional judgment**

Send only minimized fields and authorized evidence from one evaluation item. The model returns `SemanticModelJudgment`. Do not request or persist chain-of-thought.

- [ ] **Step 4: Implement deterministic evidence review and server assembly**

Validate citations belong to the item, account IDs are effective/compatible, uncertainty language is used, and model fields contain no authority override. Then assemble `SemanticDetection` from trusted evaluation/version/account values.

- [ ] **Step 5: Run judgment tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/business_entertainment/test_professional_agent.py tests/unit/business_entertainment/test_evidence_review.py -q`

Expected: PASS.

- [ ] **Step 6: Write RED routing tests**

Assert the shared SAP router saves DetectionRecord plus exactly one of no case, EvidenceTask or RiskCase in one transaction; suspected labels create one stable case for either mode; unlinked cases have null SAP fields and initial “待定位SAP凭证” workflow after confirmation; reasonable creates detection only; insufficient creates EvidenceTask; standalone SAP never reaches routing; new model version adds detection without duplicate case.

- [ ] **Step 7: Run routing tests and confirm RED**

Run: `cd backend && pytest tests/unit/semantic/test_detection_router.py tests/integration/application/test_semantic_case_routing.py -q`

Expected: FAIL before case extension.

- [ ] **Step 8: Extend phase1 case contract and implement routing**

Add monitor type, canonical source, source mode, SAP link status, risk amount/source, confidence, account dictionary version and merged target to `domain/cases.py`. Implement `route_sap_detection(detection, suspicious_labels, uow) -> RoutingResult` in the shared router: one transaction saves DetectionRecord and either EvidenceTask, no case, or calls phase1 CreateOrUpdateRisk after validating the SAP fingerprint. Business-document-unlinked routing remains in `application/cases.py`. Reuse phase1 state transition, Principal and transaction helpers.

- [ ] **Step 9: Run routing tests and confirm GREEN**

Run: `cd backend && pytest tests/unit/semantic/test_detection_router.py tests/integration/application/test_semantic_case_routing.py -q`

Expected: PASS; case keys and detection keys are idempotent.

- [ ] **Step 10: Commit Task 8**

~~~bash
git add backend/src/tax_risk/application/business_entertainment backend/src/tax_risk/application/cases.py backend/src/tax_risk/domain/cases.py backend/tests
git commit -m "feat: create evidence reviewed entertainment cases"
~~~

### Task 9: Resolve persisted exact links and prevent duplicate list, dashboard, export, and KPI totals

**Files:**

- Create: `backend/src/tax_risk/application/case_merge.py`
- Create: `backend/src/tax_risk/application/business_entertainment/reporting.py`
- Create: `backend/src/tax_risk/application/business_entertainment/export.py`
- Modify: `backend/src/tax_risk/application/cases.py`
- Modify: `backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- Modify: `backend/src/tax_risk/api/routes/dashboard.py`
- Create: `backend/src/tax_risk/api/routes/exports.py`
- Modify: `backend/src/tax_risk/api/schemas.py`
- Modify: `backend/src/tax_risk/main.py`
- Test: `backend/tests/integration/application/test_resolve_case_to_sap.py`
- Test: `backend/tests/integration/application/test_root_case_aggregations.py`
- Test: `backend/tests/integration/api/test_entertainment_export.py`

- [ ] **Step 1: Write RED exact-evidence resolution tests**

API/use case inputs are `business_case_id, evidence_link_id, expected_row_version`. Reject nonexistent, FUZZY, cross-company, wrong-source, wrong-target, wrong-snapshot or already-consumed evidence.

- [ ] **Step 2: Run resolution tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_resolve_case_to_sap.py -q`

Expected: FAIL because resolver is absent.

- [ ] **Step 3: Implement server-side revalidation and one transaction**

Lock source case and persisted link; reload both observations; validate EXACT and lineage; derive SAP case key; create or reuse root; attach history/evidence; set `merged_into_case_id`; append audit action; commit once. Retry returns the same root.

- [ ] **Step 4: Run resolution tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/application/test_resolve_case_to_sap.py -q`

Expected: PASS, including rollback after injected failure.

- [ ] **Step 5: Write RED anti-duplication tests for every consumer**

Create one unlinked case, resolve it to a new/existing SAP root, then assert:

- risk list returns one active root;
- dashboard linked/unlinked counts and amounts total one root;
- Excel export has one row and SAP root amount;
- KPI risk count/amount excludes merged source;
- source detail remains reachable for audit.

- [ ] **Step 6: Run aggregation tests and confirm RED**

Run: `cd backend && pytest tests/integration/application/test_root_case_aggregations.py tests/integration/api/test_entertainment_export.py -q`

Expected: FAIL before root-only query predicates.

- [ ] **Step 7: Implement shared root-case reporting query**

All four consumers call one repository query that enforces `merged_into_case_id IS NULL` plus Principal company scope. `export.py` exposes a pure, versioned `BusinessEntertainmentExportRow`/column-schema producer; Phase4 asynchronous export jobs must consume this producer rather than copy its query or columns. Phase2 writes escaped XLSX text, evidence references, source mode and “待定位” state without formulas from source text. Register the scoped export route in phase1 `main.py` and use transport models from `api/schemas.py`.

- [ ] **Step 8: Run aggregation tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/application/test_root_case_aggregations.py tests/integration/api/test_entertainment_export.py -q`

Expected: PASS with one count and one amount after every merge/retry scenario.

- [ ] **Step 9: Commit Task 9**

~~~bash
git add backend/src/tax_risk/application/case_merge.py backend/src/tax_risk/application/business_entertainment/reporting.py backend/src/tax_risk/application/business_entertainment/export.py backend/src/tax_risk/application/cases.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/src/tax_risk/api/routes/dashboard.py backend/src/tax_risk/api/routes/exports.py backend/src/tax_risk/api/schemas.py backend/src/tax_risk/main.py backend/tests
git commit -m "feat: merge and report entertainment risks once"
~~~

### Task 10: Expose phase1-compatible APIs, 404 scope behavior, and risk UI

**Files:**

- Create: `backend/src/tax_risk/api/routes/business_entertainment.py`
- Modify: `backend/src/tax_risk/api/routes/cases.py`
- Modify: `backend/src/tax_risk/api/schemas.py`
- Modify: `backend/src/tax_risk/main.py`
- Create: `web/src/features/risks/api.ts`
- Create: `web/src/features/risks/types.ts`
- Create: `web/src/features/risks/RiskListPage.tsx`
- Create: `web/src/features/risks/RiskDetailPage.tsx`
- Create: `web/src/features/business-entertainment/api.ts`
- Create: `web/src/features/business-entertainment/types.ts`
- Create: `web/src/features/business-entertainment/SapLinkCoveragePage.tsx`
- Modify: `web/src/App.tsx`
- Test: `backend/tests/integration/api/test_business_entertainment_api.py`
- Test: `web/src/features/risks/RiskPages.test.tsx`
- Test: `web/src/features/business-entertainment/SapLinkCoveragePage.test.tsx`

- [ ] **Step 1: Write RED API tests**

Use phase1 prefix `/api/v1`. Extend `GET /api/v1/risk-cases` filters for monitor type, source mode, SAP link status, confidence, status, company and period. Add coverage GET and resolve POST. Unauthorized/out-of-scope case or company returns 404, not 403.

- [ ] **Step 2: Run API tests and confirm RED**

Run: `cd backend && pytest tests/integration/api/test_business_entertainment_api.py -q`

Expected: FAIL with missing routes/schema fields.

- [ ] **Step 3: Implement thin routes and registration**

Routes call application services only. Detail includes canonical source, nullable SAP fields, amount source, exact/fuzzy distinction, cited snippets, suggestions, missing evidence, artifact/account versions and merge history. Register routes in phase1 `main.py`.

- [ ] **Step 4: Run API tests and confirm GREEN**

Run: `cd backend && pytest tests/integration/api/test_business_entertainment_api.py -q`

Expected: PASS, including company scope 404 and optimistic-lock 409.

- [ ] **Step 5: Write RED UI tests**

Test linked/unlinked and confidence filters, “待定位SAP凭证”, evidence citations, account suggestions, exact-link-only resolve dialog, SAP coverage semantics and Excel export action.

- [ ] **Step 6: Run UI tests and confirm RED**

Run: `npm --prefix web test -- --run src/features/risks/RiskPages.test.tsx src/features/business-entertainment/SapLinkCoveragePage.test.tsx`

Expected: FAIL because pages are absent.

- [ ] **Step 7: Implement UI and phase1 App registration**

Use TanStack Query and Ant Design. Render source text as escaped text nodes. Resolve submits only persisted evidence-link ID and row version. After merge, invalidate list, source/root detail, dashboard, export metadata and KPI queries.

- [ ] **Step 8: Run UI tests, lint and typecheck**

Run: `npm --prefix web test -- --run && npm --prefix web run lint && npm --prefix web run typecheck`

Expected: PASS with no failed tests, lint or type errors.

- [ ] **Step 9: Commit Task 10**

~~~bash
git add backend/src/tax_risk/api backend/src/tax_risk/main.py backend/tests/integration/api web/src/features web/src/App.tsx
git commit -m "feat: expose entertainment review experience"
~~~

### Task 11: Freeze dual-reviewed gold data and run security, metrics, and E2E release gates

**Files:**

- Create: `backend/tests/fixtures/business_entertainment/golden.jsonl`
- Create: `backend/tests/evaluation/test_business_entertainment_metrics.py`
- Create: `backend/tests/evaluation/test_golden_governance.py`
- Create: `backend/tests/security/test_prompt_injection.py`
- Create: `backend/tests/e2e/test_business_entertainment_pipeline.py`
- Create: `backend/tests/integration/application/test_business_entertainment_pipeline_wiring.py`
- Create: `backend/tests/unit/api/test_business_entertainment_dependency_binding.py`
- Create: `web/e2e/business-entertainment.spec.ts`
- Create: `docs/runbooks/phase-2-business-entertainment-agent.md`
- Modify: `backend/src/tax_risk/application/business_entertainment/service.py`
- Modify: `backend/src/tax_risk/workers/business_entertainment.py`
- Modify: `backend/src/tax_risk/api/routes/business_entertainment.py`
- Modify: `backend/src/tax_risk/main.py`

- [ ] **Step 1: Write RED golden-governance tests**

Each record requires stable sample ID, redacted inputs, source mode, expected evidence, finance label/annotator/time, tax label/annotator/time, distinct annotators, adjudicator/final label, approval state, frozen version/checksum and frozen time. Only APPROVED+FROZEN versions enter release metrics; frozen records are immutable.

- [ ] **Step 2: Run governance tests and confirm RED**

Run: `cd backend && pytest tests/evaluation/test_golden_governance.py -q`

Expected: FAIL before fixture validator.

- [ ] **Step 3: Implement validator and seed adjudicated examples**

Include linked training/meeting meals, valid reception, conflicting evidence, OA-only, Hesi+OA canonical, standalone SAP coverage and later SAP resolution. Include unhit samples so recall is not measured only on alerts.

- [ ] **Step 4: Run governance tests and confirm GREEN**

Run: `cd backend && pytest tests/evaluation/test_golden_governance.py -q`

Expected: PASS and checksum is stable.

- [ ] **Step 5: Write RED metrics and security tests**

Measure candidate, model and evidence-reviewed recall separately for each source mode. Require pilot recall≥90%, release recall≥95%, high-confidence accuracy≥80% and known cases zero misses. Inject instructions, PII and cross-company evidence; assert no authority/tool change, no PII retention and no unauthorized citation.

- [ ] **Step 6: Run metrics/security tests and confirm RED**

Run: `cd backend && pytest tests/evaluation/test_business_entertainment_metrics.py tests/security/test_prompt_injection.py tests/security/test_model_pii_retention_audit.py -q`

Expected: FAIL until evaluator and final safety wiring exist.

- [ ] **Step 7: Implement evaluator and final safety wiring**

Use the fake client for deterministic CI, emit machine-readable metrics, sample unhit negatives and block release when any threshold or call-audit requirement fails.

- [ ] **Step 8: Run metrics/security tests and confirm GREEN**

Run: `cd backend && pytest tests/evaluation/test_business_entertainment_metrics.py tests/security/test_prompt_injection.py tests/security/test_model_pii_retention_audit.py -q`

Expected: PASS with the four required thresholds.

- [ ] **Step 9: Write RED backend E2E**

Cover linked SAP chain, unlinked Hesi+OA, OA-only risk/evidence task, standalone SAP coverage and exact-link merge with one active amount.

- [ ] **Step 10: Run backend E2E and confirm RED**

Run: `cd backend && pytest tests/e2e/test_business_entertainment_pipeline.py -q`

Expected: FAIL at an unwired application/API boundary.

- [ ] **Step 11: Wire the application service pipeline**

Wire `service.py` in the fixed order scope gate → PUBLISHED SnapshotSet load → exact link → evaluation/coverage → candidates → governed Agent → shared/business-document router. Reject any set whose status is not PUBLISHED or whose `published_at` is null. Do not introduce another domain contract or duplicate reporting query.

- [ ] **Step 12: Verify the application service order**

Run: `cd backend && pytest tests/integration/application/test_business_entertainment_pipeline_wiring.py::test_service_orders_scope_snapshot_link_candidate_agent_and_router -q`

Expected: PASS; each stage is called once in the locked order and standalone SAP stops at coverage.

- [ ] **Step 13: Wire the Celery task arguments**

Modify `workers/business_entertainment.py` so each company task receives only run ID, company, period, PUBLISHED SnapshotSet ID and published rule/model/prompt/case-library/account-dictionary version IDs, then resolves application dependencies inside the worker process. PUBLISHED is the only runnable SnapshotSet state.

- [ ] **Step 14: Verify worker wiring**

Run: `cd backend && pytest tests/integration/workers/test_business_entertainment_worker.py::test_worker_passes_snapshot_and_published_versions -q`

Expected: PASS; retry uses the same IDs and idempotency key.

- [ ] **Step 15: Bind environment-appropriate model dependencies**

Add the FastAPI/application dependency provider that selects `EnterpriseStructuredModelClient` for production and the fake client only under the explicit test setting; startup fails when production enterprise/zero-retention configuration is incomplete.

- [ ] **Step 16: Verify dependency binding**

Run: `cd backend && pytest tests/unit/api/test_business_entertainment_dependency_binding.py -q`

Expected: PASS; production never resolves the fake client and invalid enterprise configuration fails closed.

- [ ] **Step 17: Register the final business-entertainment route**

Register `api/routes/business_entertainment.py` in phase1 `main.py`, inject only application ports and preserve `/api/v1` plus phase1 Principal dependencies.

- [ ] **Step 18: Run backend E2E and confirm GREEN**

Run: `cd backend && pytest tests/e2e/test_business_entertainment_pipeline.py -q`

Expected: PASS for all five paths and idempotent rerun.

- [ ] **Step 19: Add and run Playwright E2E**

Run: `npm --prefix web run test:e2e -- business-entertainment.spec.ts`

Expected: PASS for filters, evidence detail,待定位, exact resolve, post-merge totals and export.

- [ ] **Step 20: Write the operations runbook**

Document version publication, enterprise endpoint/zero retention, schema-failure queue, company rerun, coverage interpretation, golden refresh, export/KPI root-case rule and rollback. Do not include credentials or sensitive source text.

- [ ] **Step 21: Run complete phase2 verification**

Run:

~~~bash
cd backend
alembic upgrade head
pytest -q
cd ..
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web test -- --run
npm --prefix web run build
npm --prefix web run test:e2e -- business-entertainment.spec.ts
~~~

Expected: all commands PASS; migration chain is `0001→0002a→0002b→0002c→0002d`; scope/import/lineage/schema/security/merge/aggregation tests pass; release metrics meet the approved thresholds.

- [ ] **Step 22: Commit Task 11**

~~~bash
git add backend/tests web/e2e/business-entertainment.spec.ts docs/runbooks/phase-2-business-entertainment-agent.md
git commit -m "test: gate business entertainment agent release"
~~~

## Final Definition of Done

- [ ] Effective company scope is versioned, independently reviewed and blocking when invalid.
- [ ] SAP plus four predecessor source types enter through phase1 IngestBatch and immutable SnapshotSet lineage.
- [ ] Shared SAP observation and suggested-account dictionary are the only contracts reused by phase3.
- [ ] Exact linkage, Hesi canonical priority, two evaluation modes and standalone-SAP coverage pass.
- [ ] Candidate lexicon has a strict versioned schema and known positives have zero candidate misses.
- [ ] Model judgment contains no server-authoritative fields; SemanticDetection is assembled after evidence/account/version validation.
- [ ] Enterprise calls require published artifacts, PII minimization, zero-retention confirmation and privacy-safe call audit.
- [ ] Unlinked business documents may create formal risks with null SAP fields and待定位 status.
- [ ] Resolve uses persisted exact evidence ID, revalidates it server-side and merges transactionally.
- [ ] List, dashboard, export and KPI each count one root case/amount after merge and retry.
- [ ] Phase4 asynchronous export reuses the Phase2 root-case row/schema producer.
- [ ] APIs retain `/api/v1/risk-cases` and out-of-scope resources return 404.
- [ ] Golden samples have finance/tax dual labels, adjudication, approval, freeze and checksum.
- [ ] Pilot recall≥90%, release recall≥95%, high-confidence accuracy≥80%, known examples zero misses.
- [ ] No welfare or donation behavior is implemented.

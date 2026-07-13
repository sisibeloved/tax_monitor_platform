# 阶段2：业务招待费Agent实施计划

> **面向Agent执行者：** 必须使用 `superpowers:subagent-driven-development`（如有可用子Agent）或 `superpowers:executing-plans` 执行本计划。各步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 在阶段1确定性底座上交付业务招待费专业Agent，完整接入SAP、合思和三类OA数据，构建可审计的精确证据链，同时允许未关联OA/合思业务单据形成“SAP凭证待定位”的正式风险，且不得任意归因或重复统计风险。

**架构：** 所有来源复用阶段1 `IngestBatch → SourceRecord → AccountingSnapshot → SnapshotSet` 不可变链路。确定性服务先生成 `SAP_LINKED`、`BUSINESS_DOCUMENT_UNLINKED` 和 `SapLinkCoverage`；高召回候选再经厂商中立 `StructuredModelClient`、严格 `SemanticModelJudgment` schema和独立证据复核，最后由服务端组装权威 `SemanticDetection`。案件、驾驶舱、导出和KPI只聚合未合并根案件。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2/Alembic、PostgreSQL、Celery/Redis、pytest/Hypothesis、httpx、openpyxl；React、TypeScript、Vite、Ant Design、TanStack Query、Vitest/Testing Library、Playwright。

---

## 0. 执行约定

### 0.1 权威参考资料

- V0.8规格说明书：`docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md`。
- V0.8详细设计：`docs/design/detailed/2026-07-12-group-income-tax-risk-monitoring-platform-detailed.md`。
- 阶段1计划：`docs/superpowers/plans/2026-07-12-phase-1-foundation-quarterly.md`。
- 现有路径、API前缀、pytest调用方式、Principal范围、IngestBatch、快照、案件、持久化和Celery契约均以阶段1为准。

任务1开始前：

~~~bash
cd backend
pytest -q
cd ..
npm --prefix web test -- --run
~~~

预期：阶段1后端和Web测试全部通过（PASS）。如果阶段1测试未全部通过，应停止执行；不得另建一套并行脚手架。

### 0.2 范围与排除项

范围内：

- 版本化业务招待费公司清单，涵盖上传、复核、生效期间和阻断式质量门禁。
- SAP费用凭证及四类前置来源：OA业务招待申请、OA自采报销、OA物料领用和合思业务招待报销。
- 各数据源均使用阶段1 IngestBatch适配器、来源血缘、不可变快照和SnapshotSet读取机制。
- SAP与OA/合思的精确关联、两种语义评估模式、独立SAP覆盖检查以及高召回候选词库。
- 共享的SAP凭证语义观察、厂商中立的结构化模型端口、企业模型适配器和制品版本治理。
- 专业判断、证据校验、建议科目治理、风险案件、人工处置流程、精确关联解决、驾驶舱、Excel导出、KPI和UI。
- 双人标注黄金数据集、提示词注入/PII/零留存测试，以及后端/前端E2E测试。

范围外：

- 福利费和公益性捐赠监测逻辑。
- 模糊关联自动挂接、自动生成会计分录、在线自学习以及公共模型训练。
- 对没有精确前置单据关联的SAP凭证进行Agent判断；该类凭证仅进入 `SapLinkCoverage`。
- 替换阶段1的数据源、快照、Principal、案件、数据库会话、Celery或季度监测模块。

### 0.3 锁定的业务不变量

1. SAP自动关联必须基于直接凭证/行项目引用，或SAP分配/参考字段中的精确单据ID；金额、日期、人员相似只能判定为 `FUZZY`。
2. 与OA精确关联的合思单据作为规范单据；OA及其自采/物料单据作为证据。未关联的自采/物料单据不得独立形成风险。
3. `SAP_LINKED` 必须包含SAP标识并采用SAP金额。`BUSINESS_DOCUMENT_UNLINKED` 采用合思或OA的规范标识，允许SAP字段为空，并采用规范业务单据金额。
4. 没有精确前置单据关联的SAP凭证只生成一条覆盖观察，不进行语义评估。
5. 只有疑似错入科目标签才创建RiskCase。`CURRENT_ACCOUNT_REASONABLE` 仅保存DetectionRecord；`INSUFFICIENT_EVIDENCE` 创建EvidenceTask。
6. 模型输出不得拥有公司、期间、来源模式、规范标识、SAP引用、金额、快照或版本字段的决定权。
7. 解决请求只能提交已持久化的证据关联ID。服务端必须在合并事务中重新加载并校验 `EXACT` 质量、公司、来源、目标和快照血缘。
8. 已合并的来源案件保留用于审计，但清单、驾驶舱、导出和KPI只统计满足 `merged_into_case_id IS NULL` 的根案件。
9. `SapExpenseVoucherObservation` 是不可变的来源标准化记录，仅关联 `SourceRecord`。具有非空快照外键的独立 `SapExpenseVoucherSnapshotProjection` 在SnapshotSet发布事务中插入；后续不得更新任一实体以挂接快照。加载器返回冻结的 `SnapshotBoundSapExpenseVoucher` DTO，在不修改任何记录的前提下组合两类ID。

### 0.4 规范文件映射

阶段2创建并由阶段3复用的共享契约：

- `backend/src/tax_risk/domain/semantic/sap_voucher.py` — 来源观察、快照投影、冻结的绑定视图DTO和科目族枚举。
- `backend/src/tax_risk/domain/semantic/contracts.py` — `SemanticModelJudgment` 和由服务端掌控的 `SemanticDetection`。
- `backend/src/tax_risk/domain/semantic/account_dictionary.py` — 唯一的不可变版本化建议科目字典。
- `backend/src/tax_risk/application/semantic/model_client.py` — `StructuredModelClient` Protocol。
- `backend/src/tax_risk/application/semantic/evidence_review.py` — 共享SAP凭证EvidencePack构建器和引用解析器。
- `backend/src/tax_risk/application/semantic/detection_router.py` — 单事务SAP检测/EvidenceTask/RiskCase路由。
- `backend/src/tax_risk/application/semantic/version_registry.py` — 模型、提示词和案例库的审批/发布。
- `backend/src/tax_risk/persistence/semantic_models.py` — 共享SAP观察、制品版本、科目字典和模型调用审计ORM。
- `backend/src/tax_risk/persistence/semantic_repositories.py` — 聚焦型共享存储库。

业务招待费专用文件：

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

需要修改且不得复制的阶段1现有文件：

- `backend/pyproject.toml`
- `backend/src/tax_risk/config.py`
- `backend/src/tax_risk/domain/cases.py`
- `backend/src/tax_risk/persistence/models.py` 仅用于暴露共享Base/metadata导入；不得包含阶段2表定义主体。
- `backend/src/tax_risk/persistence/repositories.py` 仅用于复用阶段1会话/工作单元辅助函数。
- `backend/src/tax_risk/application/ingest.py`
- `backend/src/tax_risk/application/snapshots.py`
- `backend/src/tax_risk/api/schemas.py`
- `backend/src/tax_risk/api/routes/cases.py`
- `backend/src/tax_risk/api/routes/dashboard.py`
- `backend/src/tax_risk/main.py`
- `backend/src/tax_risk/workers/celery_app.py`
- `backend/migrations/env.py`
- `web/src/App.tsx`

迁移链以当前阶段1真实迁移头 `0006_review_action_assignee` 为起点，禁止从早期 `0001_control_plane` 分叉：

| 文件 | `revision` | `down_revision` |
|---|---|---|
| `0007_business_entertainment_scope.py` | `0007_entertainment_scope` | `0006_review_action_assignee` |
| `0008_business_entertainment_observations.py` | `0008_entertainment_observations` | `0007_entertainment_scope` |
| `0008a_entertainment_snapshot_guard.py` | `0008a_ent_snapshot_guard` | `0008_entertainment_observations` |
| `0009_semantic_contracts_accounts.py` | `0009_semantic_accounts` | `0008a_ent_snapshot_guard` |
| `0010_semantic_artifacts_calls.py` | `0010_semantic_artifacts` | `0009_semantic_accounts` |
| 阶段3 `0011_welfare_donation_agents.py` | `0011_welfare_donation` | `0010_semantic_artifacts` |

## 工作块1：受控数据源、精确证据与高召回候选

### 任务1：新增版本化业务招待费公司清单和阻断式质量门禁

**文件：**

- 新建：`backend/src/tax_risk/domain/business_entertainment/company_scope.py`
- 新建：`backend/src/tax_risk/adapters/ingest/business_entertainment_company_list_xlsx.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/company_scope.py`
- 修改：`backend/src/tax_risk/application/ingest.py`
- 新建：`backend/src/tax_risk/persistence/business_entertainment_models.py`
- 新建：`backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- 新建：`backend/migrations/versions/0007_business_entertainment_scope.py`
- 修改：`backend/migrations/env.py`
- 测试：`backend/tests/unit/adapters/test_business_entertainment_company_list_xlsx.py`
- 测试：`backend/tests/integration/application/test_business_entertainment_company_scope.py`

- [ ] **步骤1：编写导入和有效版本的RED测试**

测试必填列 `company_code, effective_from, effective_to`；拒绝空白/未知/重复公司、无效期间和已发布版本的期间重叠。断言同一人员不能同时担任上传人和复核人。

- [ ] **步骤2：运行RED测试**

运行：`cd backend && pytest tests/unit/adapters/test_business_entertainment_company_list_xlsx.py tests/integration/application/test_business_entertainment_company_scope.py -q`

预期：由于导入器、模型和服务尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现不可变范围契约**

定义：

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

持久化版本头和公司行，确保 `version_id + company_code` 唯一、已发布版本的有效期间互不重叠，并通过外键校验阶段1 Company。

- [ ] **步骤4：实现上传、复核、发布和质量门禁**

XLSX适配器首先创建阶段1 IngestBatch/SourceRecord血缘，随后门禁针对所请求月份返回唯一一个已发布的有效版本。版本缺失、重复、重叠或未审批时创建DataIssue，并且仅阻断业务招待费监测；不得静默推断任何公司。

- [ ] **步骤5：运行GREEN测试**

运行：`cd backend && alembic upgrade head && pytest tests/unit/adapters/test_business_entertainment_company_list_xlsx.py tests/integration/application/test_business_entertainment_company_scope.py -q`

预期：测试通过（PASS）；有效公司范围具有确定性，被拒绝的版本无法运行。

- [ ] **步骤6：提交任务1**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/company_scope.py backend/src/tax_risk/adapters/ingest/business_entertainment_company_list_xlsx.py backend/src/tax_risk/application/business_entertainment/company_scope.py backend/src/tax_risk/application/ingest.py backend/src/tax_risk/persistence/business_entertainment_models.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/migrations/versions/0007_business_entertainment_scope.py backend/migrations/env.py backend/tests
git commit -m "feat: govern entertainment company scope"
~~~

### 任务2：通过阶段1 IngestBatch定义并采集全部五类源数据集

**文件：**

- 新建：`backend/src/tax_risk/domain/semantic/sap_voucher.py`
- 新建：`backend/src/tax_risk/domain/business_entertainment/source_models.py`
- 新建：0.4节列出的五个CSV适配器
- 修改：`backend/src/tax_risk/application/ingest.py`
- 新建：`backend/src/tax_risk/persistence/semantic_models.py`
- 新建：`backend/src/tax_risk/persistence/semantic_repositories.py`
- 修改：`backend/src/tax_risk/persistence/business_entertainment_models.py`
- 新建：`backend/migrations/versions/0008_business_entertainment_observations.py`
- 修改：`backend/migrations/env.py`
- 测试：`backend/tests/unit/adapters/test_business_entertainment_source_adapters.py`
- 测试：`backend/tests/integration/application/test_business_entertainment_ingest.py`

- [ ] **步骤1：为每类数据源编写schema的RED测试**

锁定以下字段和约束：

- SAP：公司、会计年度、期间、过账日期、凭证、行项目、当前科目、Decimal金额、币种、摘要、分配/参考、冲销参考、科目族；主键为公司+年度+凭证+行项目。
- 合思：公司、年度、期间、报销单ID、行ID、日期、Decimal金额、币种、摘要、报销事由、接待对象类别、参与人数、关联OA ID、可选的直接SAP凭证/行项目；主键为公司+报销单+行项目。
- OA业务招待申请：公司、申请单ID、行ID、日期、事由、接待对象类别、参与人数、Decimal申请金额/币种；主键为公司+申请单+行项目。
- OA自采：公司、申请单ID、行ID、日期、物品、事由、接收对象类别、Decimal金额/币种、精确的上级OA/合思ID；主键为公司+申请单+行项目。
- OA物料领用：公司、领用单ID、行ID、日期、物料、用途、接收对象类别、数量/单位、可选的Decimal金额/币种、精确的上级OA/合思ID；主键为公司+领用单+行项目。

所有Pydantic模型均使用 `extra="forbid"`。标识和期间字段不得为空；SAP冲销金额允许为负数；标准化语义schema不得接收参与人姓名、电话号码或身份证件号码。

- [ ] **步骤2：运行schema测试并确认RED状态**

运行：`cd backend && pytest tests/unit/adapters/test_business_entertainment_source_adapters.py -q`

预期：由于契约/适配器缺失，测试收集失败（FAIL）。

- [ ] **步骤3：基于阶段1 AdapterResult实现适配器**

每个适配器输出源schema版本、源主键定义、接收/拒绝数量、Decimal控制总额和逐行错误。在阶段1 `application/ingest.py` 中注册数据集类型；不得绕过IngestBatch或直接写入分析表。

- [ ] **步骤4：运行schema测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/adapters/test_business_entertainment_source_adapters.py -q`

预期：有效、重复、格式错误以及PII拒绝fixture均测试通过（PASS）。

- [ ] **步骤5：编写血缘集成的RED测试**

断言每条标准化观察均引用 `ingest_batch_id` 和 `source_record_id`；`PARTIAL` 批次仍处于未就绪状态；重复源主键被拒绝；每类数据源的控制总额均核对一致。

- [ ] **步骤6：运行血缘测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_business_entertainment_ingest.py -q`

预期：在存储库完成接线前测试失败（FAIL）。

- [ ] **步骤7：持久化聚焦型来源索引**

`SapExpenseVoucherObservation` 位于 `semantic_models.py`，包含UUID、非空 `source_record` 外键、源主键、`created_at`、公司/年度/期间、凭证/行项目/科目、Decimal金额/币种、冲销参考和 `account_family=BUSINESS_ENTERTAINMENT`；不包含快照外键。`SapExpenseVoucherSnapshotProjection(id, observation_id, snapshot_id, company_code, period, created_at)` 具有非空外键，以快照+观察作为唯一约束，并且插入后不可变。领域层定义冻结的 `SnapshotBoundSapExpenseVoucher`，包含观察字段以及投影ID、快照ID和源记录ID；它是读取DTO，而非另一张表。OA/合思来源索引同样只关联SourceRecord。同一迁移还定义：`evidence_link(id, company_code, source_record_id, target_record_id, relation_kind, relation_quality, matched_field, snapshot_id, created_at)`，以快照+来源+目标+类型作为唯一约束；`business_entertainment_evaluation(id, candidate_key, company_code, fiscal_year, period, source_mode, canonical_record_type, canonical_source_record_id, sap_observation_id nullable, amount, amount_source, snapshot_id, created_at)`，以快照+候选键作为唯一约束；以及 `sap_link_coverage(id, company_code, period, sap_observation_id, link_status, exact_evidence_link_id nullable, evaluated_via_business_document, snapshot_id, created_at)`，以快照+SAP观察作为唯一约束。不得存储重复的原始payload。

- [ ] **步骤8：运行血缘测试并确认GREEN状态**

运行：`cd backend && alembic upgrade head && pytest tests/integration/application/test_business_entertainment_ingest.py -q`

预期：测试通过（PASS）；五类数据集均可追溯至IngestBatch/SourceRecord。

- [ ] **步骤9：提交任务2**

~~~bash
git add backend/src/tax_risk/domain backend/src/tax_risk/adapters/ingest backend/src/tax_risk/application/ingest.py backend/src/tax_risk/persistence backend/migrations/versions/0008_business_entertainment_observations.py backend/migrations/env.py backend/tests/unit/adapters/test_business_entertainment_source_adapters.py backend/tests/integration/application/test_business_entertainment_ingest.py
git commit -m "feat: ingest entertainment evidence with lineage"
~~~

### 任务3：加载不可变快照并建立精确关联，避免任意归因

**文件：**

- 新建：`backend/src/tax_risk/application/business_entertainment/source_loader.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/linker.py`
- 修改：`backend/src/tax_risk/application/snapshots.py`
- 修改：`backend/src/tax_risk/persistence/semantic_models.py`
- 修改：`backend/src/tax_risk/persistence/semantic_repositories.py`
- 修改：`backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- 测试：`backend/tests/unit/business_entertainment/test_exact_linker.py`
- 测试：`backend/tests/integration/application/test_entertainment_snapshot_loader.py`

- [ ] **步骤1：编写不可变加载器的RED测试**

要求一个 `PUBLISHED` SnapshotSet包含所有必需的公司/期间来源成员，并具有非空UTC `published_at`。在同一个通过完整质量门禁的发布事务中，插入全部具有非空快照ID的 `SapExpenseVoucherSnapshotProjection` 行，将SnapshotSet转换为 `PUBLISHED`，仅写入一次 `published_at`，并使集合/成员/投影均不可变。`DRAFT`/`VALIDATED`、成员缺失或可变时返回DataIssue，且不生成评估输入。从1月至目标月份的读取必须受 `PUBLISHED` SnapshotSet约束，而非读取当前源表。测试必须拒绝发布前读取、空快照投影、发布后的UPDATE/DELETE，以及此后任何将观察挂接到快照的尝试。

- [ ] **步骤2：运行加载器测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_entertainment_snapshot_loader.py -q`

预期：由于加载器尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现仅基于SnapshotSet的数据源加载**

复用阶段1的快照成员关系和公司范围。在阶段1发布事务中，将成员SourceRecord解析为不可变观察，插入快照专属投影，将完整集合转换为 `PUBLISHED`，写入UTC `published_at`，最后一次性提交。冻结存储库签名 `load_snapshot_bound_sap_vouchers(snapshot_set_id, account_family, company_code, period_end) -> list[SnapshotBoundSapExpenseVoucher]`；它拒绝所有非 `PUBLISHED` 集合，并且只读取属于所请求 `PUBLISHED` 集合的投影。不得将缺失数据推断为零或安全空值，也不得通过修改观察来增加快照。

- [ ] **步骤4：运行加载器测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/application/test_entertainment_snapshot_loader.py -q`

预期：测试通过（PASS）；`DRAFT`/`VALIDATED` 集合无法加载，后续源批次变化不会改变已加载的 `PUBLISHED` 集合。

- [ ] **步骤5：编写精确关联的RED测试**

覆盖直接SAP凭证/行项目、SAP分配/参考中的精确单据ID、合思→OA规范单据优先级、自采/物料的精确上级证据、跨公司拒绝、重复参考导致的歧义，以及仅基于金额/日期/人员的 `FUZZY` 提示。

- [ ] **步骤6：运行关联器测试并确认RED状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_exact_linker.py -q`

预期：由于关联器尚不存在，测试失败（FAIL）。

- [ ] **步骤7：实现确定性关联器**

返回精确关联、模糊提示、冲突、未关联SAP键和未关联规范业务键。精确关联持久化来源/目标记录ID、关系类型、关系质量、匹配字段、快照ID和 `created_at`。`FUZZY` 关联绝不能作为风险证据。

- [ ] **步骤8：运行关联器测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_exact_linker.py -q`

预期：测试通过（PASS），包括Hypothesis输入顺序不变性测试。

- [ ] **步骤9：提交任务3**

~~~bash
git add backend/src/tax_risk/application/business_entertainment/source_loader.py backend/src/tax_risk/application/business_entertainment/linker.py backend/src/tax_risk/application/snapshots.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/tests
git commit -m "feat: link immutable entertainment evidence"
~~~

### 任务4：构建两种评估模式和独立SAP覆盖检查

**文件：**

- 新建：`backend/src/tax_risk/domain/business_entertainment/evaluation.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/evaluation_items.py`
- 修改：`backend/src/tax_risk/persistence/business_entertainment_models.py`
- 修改：`backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- 测试：`backend/tests/unit/business_entertainment/test_evaluation_items.py`
- 测试：`backend/tests/integration/persistence/test_sap_link_coverage.py`

- [ ] **步骤1：编写评估逻辑的RED测试**

断言精确SAP链生成一条 `SAP_LINKED` 项；没有SAP的精确合思/OA链生成一条以合思为规范单据的 `BUSINESS_DOCUMENT_UNLINKED` 项；独立OA/合思成为未关联项；仅有自采/物料单据不能成为规范单据；独立SAP只生成覆盖记录。

- [ ] **步骤2：运行评估测试并确认RED状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_evaluation_items.py -q`

预期：由于契约/构建器尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现不可变评估契约和构建器**

包含候选键、公司/年度/期间、来源模式、规范类型/键、可为空的SAP键/凭证/行项目/科目、Decimal金额、金额来源、精确证据ID和快照ID。强制SAP字段只能在已关联模式中出现。

- [ ] **步骤4：运行评估测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_evaluation_items.py -q`

预期：测试通过（PASS）。

- [ ] **步骤5：编写覆盖记录持久化的RED测试**

断言每条SAP业务招待费观察在每个快照中只生成一条 `LINKED`/`UNLINKED` 覆盖记录，未关联行设置 `evaluated_via_business_document=false`，并且重复运行具有幂等性。

- [ ] **步骤6：运行覆盖记录测试并确认RED状态**

运行：`cd backend && pytest tests/integration/persistence/test_sap_link_coverage.py -q`

预期：在覆盖记录ORM/存储库实现前测试失败（FAIL）。

- [ ] **步骤7：实现覆盖记录持久化**

存储公司、期间、SAP观察ID、凭证/行项目、Decimal金额、关联状态、存在时的精确证据关联ID、评估标志、快照ID和 `created_at`，并设置快照+SAP观察唯一约束。

- [ ] **步骤8：运行覆盖记录测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/persistence/test_sap_link_coverage.py -q`

预期：测试通过（PASS），重复写入不会改变记录数量。

- [ ] **步骤9：提交任务4**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/evaluation.py backend/src/tax_risk/application/business_entertainment/evaluation_items.py backend/src/tax_risk/persistence/business_entertainment_models.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/tests/unit/business_entertainment/test_evaluation_items.py backend/tests/integration/persistence/test_sap_link_coverage.py
git commit -m "feat: build entertainment evaluation modes"
~~~

### 任务5：新增版本化高召回词库和幂等月度Worker

**文件：**

- 新建：`backend/src/tax_risk/domain/business_entertainment/lexicon.py`
- 新建：`backend/src/tax_risk/rules/business_entertainment_candidate_lexicon.v1.yaml`
- 新建：`backend/src/tax_risk/application/business_entertainment/candidates.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/service.py`
- 新建：`backend/src/tax_risk/workers/business_entertainment.py`
- 修改：`backend/src/tax_risk/workers/celery_app.py`
- 测试：`backend/tests/unit/business_entertainment/test_candidate_lexicon.py`
- 测试：`backend/tests/integration/workers/test_business_entertainment_worker.py`

- [ ] **步骤1：编写词库schema和召回率的RED测试**

每个YAML版本包含 `version, monitor_type, effective_from, status`，词条包含 `signal_id, canonical_phrase, aliases, allowed_fields, priority, label_hints`。未知键、重复ID和空别名均应失败。纳入V0.8全部信号；任何否定词都不得抑制正向命中。

- [ ] **步骤2：运行词库测试并确认RED状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_candidate_lexicon.py -q`

预期：由于schema/文件/生成器尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现版本加载器和候选并集**

标准化标点符号和空白字符，保留引用字段/文本区间，合并全部命中结果，并保留一条低优先级全量扫描评估通道。候选输出只是筛查记录，绝不能作为最终会计判断结论。

- [ ] **步骤4：运行词库测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_candidate_lexicon.py -q`

预期：测试通过（PASS）；每个已知正例至少生成一个候选。

- [ ] **步骤5：编写Worker的RED测试**

验证有效公司清单、从1月至目标月份且具有不可变 `published_at` 的 `PUBLISHED` SnapshotSet、按公司隔离、稳定幂等键、先覆盖检查后Agent的顺序，以及独立SAP记录不进入Agent调用。

- [ ] **步骤6：运行Worker测试并确认RED状态**

运行：`cd backend && pytest tests/integration/workers/test_business_entertainment_worker.py -q`

预期：由于Worker尚未注册，测试失败（FAIL）。

- [ ] **步骤7：实现轻量Celery编排**

复用阶段1批次状态、重试分类和公司隔离机制。返回范围、数据源、精确/模糊/冲突关联、两种评估模式、独立SAP覆盖、候选、检测、证据任务和风险的数量。

- [ ] **步骤8：运行Worker测试和工作块1回归测试**

运行：

~~~bash
cd backend
pytest tests/unit/business_entertainment tests/unit/adapters tests/integration/application tests/integration/persistence tests/integration/workers/test_business_entertainment_worker.py -q
~~~

预期：测试通过（PASS）；单家公司失败不会阻断其他公司，重复运行不会产生重复记录。

- [ ] **步骤9：提交任务5**

~~~bash
git add backend/src/tax_risk/domain/business_entertainment/lexicon.py backend/src/tax_risk/rules backend/src/tax_risk/application/business_entertainment backend/src/tax_risk/workers backend/tests
git commit -m "feat: generate versioned entertainment candidates"
~~~

## 工作块2：受治理的结构化Agent、人工案件、报表与发布门禁

### 任务6：分离模型判断与服务端检测，并治理建议科目

**文件：**

- 新建：`backend/src/tax_risk/domain/semantic/contracts.py`
- 新建：`backend/src/tax_risk/domain/semantic/account_dictionary.py`
- 新建：`backend/src/tax_risk/application/semantic/model_client.py`
- 新建：`backend/src/tax_risk/application/semantic/evidence_review.py`
- 新建：`backend/src/tax_risk/application/semantic/account_dictionary.py`
- 新建：`backend/src/tax_risk/adapters/ingest/suggested_account_dictionary_xlsx.py`
- 修改：`backend/src/tax_risk/application/ingest.py`
- 修改：`backend/src/tax_risk/persistence/semantic_models.py`
- 修改：`backend/src/tax_risk/persistence/semantic_repositories.py`
- 新建：`backend/migrations/versions/0009_semantic_contracts_accounts.py`
- 测试：`backend/tests/unit/semantic/test_contract_separation.py`
- 测试：`backend/tests/unit/semantic/test_sap_voucher_evidence_pack.py`
- 测试：`backend/tests/integration/application/test_account_dictionary_governance.py`

- [ ] **步骤1：编写契约分离的RED测试**

断言 `SemanticModelJudgment` 拒绝公司、SAP、金额、快照和版本字段。断言缺少服务端所有的标识、已验证引用和 `account_dictionary_version` 时无法构建 `SemanticDetection`。

- [ ] **步骤2：运行契约测试并确认RED状态**

运行：`cd backend && pytest tests/unit/semantic/test_contract_separation.py -q`

预期：由于契约尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现严格分离**

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

`SemanticVersionSet` 是不可变服务端结构，包含规则、模型、提示词、案例库和科目字典版本ID。`SemanticDetection` 在校验后添加全部由服务端掌控的候选/公司/期间/模式/来源/SAP/金额/证据/版本/时间字段；绝不从模型响应中接收这些字段。

- [ ] **步骤4：运行契约测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/semantic/test_contract_separation.py -q`

预期：测试通过（PASS）；恶意标识字段无法通过schema校验。

- [ ] **步骤5：编写共享SAP证据的RED测试**

断言 `build_sap_voucher_evidence_pack(view, versions)` 只从一个冻结的 `SnapshotBoundSapExpenseVoucher` 输出获准的标准化SAP字段、来源/快照引用和稳定证据ID。断言 `resolve_citations(judgment, evidence_pack)` 拒绝外部ID、错误字段和被篡改的引文。

- [ ] **步骤6：运行共享证据测试并确认RED状态**

运行：`cd backend && pytest tests/unit/semantic/test_sap_voucher_evidence_pack.py -q`

预期：由于共享构建器/解析器尚不存在，测试失败（FAIL）。

- [ ] **步骤7：实现可复用的证据构建机制**

冻结函数签名 `build_sap_voucher_evidence_pack(view: SnapshotBoundSapExpenseVoucher, versions: SemanticVersionSet) -> EvidencePack` 和 `resolve_citations(judgment: SemanticModelJudgment, evidence_pack: EvidencePack) -> list[EvidenceRef]`。EvidencePack以及后续SemanticDetection从该视图取得权威快照/来源ID。业务招待费可在共享SAP证据包之后附加精确OA/合思证据；阶段3复用仅含SAP的构建器，且不得新增ORM转换方法。

- [ ] **步骤8：运行共享证据测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/semantic/test_sap_voucher_evidence_pack.py -q`

预期：测试通过（PASS）；外部或被修改的引用均被拒绝。

- [ ] **步骤9：编写建议科目治理的RED测试**

字典行包含字典版本、科目ID/编码/名称、会计分类、允许的监测类型/标签、有效期间和状态。要求上传人与复核人分离、校验和、审批和不可变发布。未知或未发布ID必须被拒绝。

- [ ] **步骤10：运行科目测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_account_dictionary_governance.py -q`

预期：由于导入/治理功能尚不存在，测试失败（FAIL）。

- [ ] **步骤11：实现唯一的共享权威字典**

通过阶段1 IngestBatch/SourceRecord导入XLSX，只有复核后才能发布版本，并提供不可变查询。为阶段2预置会议费、职工教育经费、职工福利费和人工复核类别。阶段3必须修改同一个文件/版本模型，不得另建字典。

- [ ] **步骤12：运行迁移和科目测试**

运行：`cd backend && alembic upgrade head && pytest tests/integration/application/test_account_dictionary_governance.py -q`

预期：测试通过（PASS）；只有有效且已发布的ID才能进入检测结果。

- [ ] **步骤13：提交任务6**

~~~bash
git add backend/src/tax_risk/domain/semantic backend/src/tax_risk/application/semantic backend/src/tax_risk/application/ingest.py backend/src/tax_risk/adapters/ingest/suggested_account_dictionary_xlsx.py backend/src/tax_risk/persistence/semantic_models.py backend/src/tax_risk/persistence/semantic_repositories.py backend/migrations/versions/0009_semantic_contracts_accounts.py backend/tests
git commit -m "feat: govern semantic decisions and accounts"
~~~

### 任务7：新增企业模型适配器、制品发布、PII最小化和调用审计

**文件：**

- 新建：`backend/src/tax_risk/application/semantic/version_registry.py`
- 新建：`backend/src/tax_risk/application/semantic/prompt_safety.py`
- 新建：`backend/src/tax_risk/adapters/model/enterprise_structured_client.py`
- 新建：`backend/src/tax_risk/adapters/model/fake_structured_client.py`
- 新建：`backend/src/tax_risk/api/routes/semantic_governance.py`
- 修改：`backend/src/tax_risk/config.py`
- 修改：`backend/src/tax_risk/persistence/semantic_models.py`
- 修改：`backend/src/tax_risk/persistence/semantic_repositories.py`
- 新建：`backend/migrations/versions/0010_semantic_artifacts_calls.py`
- 修改：`backend/src/tax_risk/api/schemas.py`
- 修改：`backend/src/tax_risk/main.py`
- 修改：`backend/pyproject.toml`
- 测试：`backend/tests/unit/adapters/test_enterprise_structured_client.py`
- 测试：`backend/tests/integration/application/test_semantic_version_registry.py`
- 测试：`backend/tests/security/test_model_pii_retention_audit.py`

- [ ] **步骤1：编写制品版本的RED测试**

模型、提示词和案例库制品必须包含类型、版本、校验和、存储引用/部署ID、状态、上传人、独立复核人、发布时间和有效期间。运行时拒绝 `DRAFT`/`RETIRED` 状态或不匹配的版本。

- [ ] **步骤2：运行版本测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_semantic_version_registry.py -q`

预期：由于版本注册表尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现审批/发布和持久化**

持久化不可变制品记录和活动版本集。发布操作使用阶段1 Principal和审计上下文；每种制品类型/有效期间只能有一个活动版本。SemanticDetection存储模型、提示词和案例库版本以及科目字典版本。

- [ ] **步骤4：运行版本测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/application/test_semantic_version_registry.py -q`

预期：测试通过（PASS）。

- [ ] **步骤5：编写企业适配器/安全性的RED测试**

要求配置企业HTTPS端点、部署、超时、凭据引用和 `zero_retention_required=true`。断言电话、身份证件号码和参与人姓名绝不进入输入；源文本仅保留在 `input_json` 中；提示词注入不能增加工具；日志/审计不得包含原始文本。

- [ ] **步骤6：运行适配器/安全性测试并确认RED状态**

运行：`cd backend && alembic upgrade head && pytest tests/unit/adapters/test_enterprise_structured_client.py tests/security/test_model_pii_retention_audit.py -q`

预期：由于适配器、最小化器和调用审计尚不存在，测试失败（FAIL）。

- [ ] **步骤7：实现企业适配器和伪适配器**

企业适配器将共享Protocol映射到受控端点，请求严格JSON schema，按照企业契约禁用公共训练/留存，且不提供数据库工具。伪适配器仅用于确定性测试，在生产环境中必须禁用。

- [ ] **步骤8：持久化隐私安全的调用审计**

存储调用ID、候选键、公司、制品版本、请求/输出校验和、允许字段清单、token数量、延迟、schema状态、重试次数、留存策略确认、操作人/运行ID和时间戳。绝不存储完整源文本、姓名、电话、身份证件号码、提示词正文或模型思维链。

- [ ] **步骤9：运行适配器/安全性测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/adapters/test_enterprise_structured_client.py tests/security/test_model_pii_retention_audit.py -q`

预期：测试通过（PASS）；零留存确认失败时阻断调用，且不创建风险。

- [ ] **步骤10：提交任务7**

~~~bash
git add backend/src/tax_risk/application/semantic backend/src/tax_risk/adapters/model backend/src/tax_risk/api/routes/semantic_governance.py backend/src/tax_risk/api/schemas.py backend/src/tax_risk/main.py backend/src/tax_risk/config.py backend/src/tax_risk/persistence/semantic_models.py backend/src/tax_risk/persistence/semantic_repositories.py backend/migrations/versions/0010_semantic_artifacts_calls.py backend/pyproject.toml backend/tests
git commit -m "feat: govern enterprise semantic model calls"
~~~

### 任务8：实现专业判断、独立证据复核和案件路由

**文件：**

- 新建：`backend/src/tax_risk/application/business_entertainment/agent.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/evidence_review.py`
- 修改：`backend/src/tax_risk/application/business_entertainment/service.py`
- 新建：`backend/src/tax_risk/application/cases.py`
- 新建：`backend/src/tax_risk/application/semantic/detection_router.py`
- 修改：`backend/src/tax_risk/domain/cases.py`
- 测试：`backend/tests/unit/business_entertainment/test_professional_agent.py`
- 测试：`backend/tests/unit/business_entertainment/test_evidence_review.py`
- 测试：`backend/tests/unit/semantic/test_detection_router.py`
- 测试：`backend/tests/integration/application/test_semantic_case_routing.py`

- [ ] **步骤1：编写判断/复核的RED测试**

覆盖内部培训/会议用餐、员工聚餐、有效外部接待、证据冲突、外部引用、不受支持的科目、确定性措辞以及来源模式/金额篡改。

- [ ] **步骤2：运行判断测试并确认RED状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_professional_agent.py tests/unit/business_entertainment/test_evidence_review.py -q`

预期：由于Agent/复核器尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现单项目专业判断**

仅发送来自单个评估项的最小化字段和获准证据。模型返回 `SemanticModelJudgment`。不得请求或持久化思维链。

- [ ] **步骤4：实现确定性证据复核和服务端组装**

校验引用属于当前项目、科目ID有效且兼容、使用不确定性措辞，并且模型字段不包含权限越权。随后使用可信的评估/版本/科目值组装 `SemanticDetection`。

- [ ] **步骤5：运行判断测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/business_entertainment/test_professional_agent.py tests/unit/business_entertainment/test_evidence_review.py -q`

预期：测试通过（PASS）。

- [ ] **步骤6：编写路由的RED测试**

断言共享SAP路由器在一个事务中保存DetectionRecord，并且在不创建案件、EvidenceTask、RiskCase三种结果中严格选择一种；两种模式下的疑似标签均创建一个稳定案件；未关联案件的SAP字段为空，确认后进入初始“待定位SAP凭证”流程；合理项目仅创建检测结果；证据不足时创建EvidenceTask；独立SAP绝不进入路由；新模型版本增加检测结果但不重复创建案件。

- [ ] **步骤7：运行路由测试并确认RED状态**

运行：`cd backend && pytest tests/unit/semantic/test_detection_router.py tests/integration/application/test_semantic_case_routing.py -q`

预期：在案件契约扩展前测试失败（FAIL）。

- [ ] **步骤8：扩展阶段1案件契约并实现路由**

在 `domain/cases.py` 中增加监测类型、规范来源、来源模式、SAP关联状态、风险金额/来源、置信度、科目字典版本和合并目标。在共享路由器中实现 `route_sap_detection(detection, suspicious_labels, uow) -> RoutingResult`：在一个事务中保存DetectionRecord，并且在创建EvidenceTask、不创建案件、或校验SAP指纹后调用阶段1 CreateOrUpdateRisk三种结果中选择一种。业务单据未关联路由仍保留在 `application/cases.py`。复用阶段1状态转换、Principal和事务辅助函数。

- [ ] **步骤9：运行路由测试并确认GREEN状态**

运行：`cd backend && pytest tests/unit/semantic/test_detection_router.py tests/integration/application/test_semantic_case_routing.py -q`

预期：测试通过（PASS）；案件键和检测键均具有幂等性。

- [ ] **步骤10：提交任务8**

~~~bash
git add backend/src/tax_risk/application/business_entertainment backend/src/tax_risk/application/cases.py backend/src/tax_risk/domain/cases.py backend/tests
git commit -m "feat: create evidence reviewed entertainment cases"
~~~

### 任务9：通过已持久化的精确关联解决案件，并防止清单、驾驶舱、导出和KPI重复统计

**文件：**

- 新建：`backend/src/tax_risk/application/case_merge.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/reporting.py`
- 新建：`backend/src/tax_risk/application/business_entertainment/export.py`
- 修改：`backend/src/tax_risk/application/cases.py`
- 修改：`backend/src/tax_risk/persistence/business_entertainment_repositories.py`
- 修改：`backend/src/tax_risk/api/routes/dashboard.py`
- 新建：`backend/src/tax_risk/api/routes/exports.py`
- 修改：`backend/src/tax_risk/api/schemas.py`
- 修改：`backend/src/tax_risk/main.py`
- 测试：`backend/tests/integration/application/test_resolve_case_to_sap.py`
- 测试：`backend/tests/integration/application/test_root_case_aggregations.py`
- 测试：`backend/tests/integration/api/test_entertainment_export.py`

- [ ] **步骤1：编写精确证据解决流程的RED测试**

API/用例输入为 `business_case_id, evidence_link_id, expected_row_version`。拒绝不存在、`FUZZY`、跨公司、来源错误、目标错误、快照错误或已使用的证据。

- [ ] **步骤2：运行解决流程测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_resolve_case_to_sap.py -q`

预期：由于解决器尚不存在，测试失败（FAIL）。

- [ ] **步骤3：实现服务端重新校验和单事务处理**

锁定来源案件和已持久化关联；重新加载两端观察；校验 `EXACT` 和血缘；派生SAP案件键；创建或复用根案件；附加历史/证据；设置 `merged_into_case_id`；追加审计操作；一次性提交。重试时返回同一个根案件。

- [ ] **步骤4：运行解决流程测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/application/test_resolve_case_to_sap.py -q`

预期：测试通过（PASS），包括注入故障后的回滚场景。

- [ ] **步骤5：为每个消费端编写防重复的RED测试**

创建一个未关联案件，将其解决到新建/已有SAP根案件，然后断言：

- 风险清单只返回一个活动根案件；
- 驾驶舱已关联/未关联数量和金额合计只对应一个根案件；
- Excel导出只有一行，并采用SAP根案件金额；
- KPI风险数量/金额排除已合并来源案件；
- 来源案件详情仍可访问以供审计。

- [ ] **步骤6：运行聚合测试并确认RED状态**

运行：`cd backend && pytest tests/integration/application/test_root_case_aggregations.py tests/integration/api/test_entertainment_export.py -q`

预期：在仅查询根案件的谓词实现前测试失败（FAIL）。

- [ ] **步骤7：实现共享根案件报表查询**

四个消费端统一调用一个存储库查询，该查询强制应用 `merged_into_case_id IS NULL` 和Principal公司范围。`export.py` 提供纯函数式、版本化的 `BusinessEntertainmentExportRow`/列schema生成器；阶段4异步导出任务必须使用该生成器，不得复制其查询或列定义。阶段2写入经过转义的XLSX文本、证据引用、来源模式和“待定位”状态，不得根据源文本生成公式。在阶段1 `main.py` 中注册受范围控制的导出路由，并使用 `api/schemas.py` 中的传输模型。

- [ ] **步骤8：运行聚合测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/application/test_root_case_aggregations.py tests/integration/api/test_entertainment_export.py -q`

预期：测试通过（PASS）；每种合并/重试场景后均只统计一次数量和一次金额。

- [ ] **步骤9：提交任务9**

~~~bash
git add backend/src/tax_risk/application/case_merge.py backend/src/tax_risk/application/business_entertainment/reporting.py backend/src/tax_risk/application/business_entertainment/export.py backend/src/tax_risk/application/cases.py backend/src/tax_risk/persistence/business_entertainment_repositories.py backend/src/tax_risk/api/routes/dashboard.py backend/src/tax_risk/api/routes/exports.py backend/src/tax_risk/api/schemas.py backend/src/tax_risk/main.py backend/tests
git commit -m "feat: merge and report entertainment risks once"
~~~

### 任务10：提供兼容阶段1的API、404范围行为和风险UI

**文件：**

- 新建：`backend/src/tax_risk/api/routes/business_entertainment.py`
- 修改：`backend/src/tax_risk/api/routes/cases.py`
- 修改：`backend/src/tax_risk/api/schemas.py`
- 修改：`backend/src/tax_risk/main.py`
- 新建：`web/src/features/risks/api.ts`
- 新建：`web/src/features/risks/types.ts`
- 新建：`web/src/features/risks/RiskListPage.tsx`
- 新建：`web/src/features/risks/RiskDetailPage.tsx`
- 新建：`web/src/features/business-entertainment/api.ts`
- 新建：`web/src/features/business-entertainment/types.ts`
- 新建：`web/src/features/business-entertainment/SapLinkCoveragePage.tsx`
- 修改：`web/src/App.tsx`
- 测试：`backend/tests/integration/api/test_business_entertainment_api.py`
- 测试：`web/src/features/risks/RiskPages.test.tsx`
- 测试：`web/src/features/business-entertainment/SapLinkCoveragePage.test.tsx`

- [ ] **步骤1：编写API的RED测试**

使用阶段1前缀 `/api/v1`。扩展 `GET /api/v1/risk-cases` 过滤条件，支持监测类型、来源模式、SAP关联状态、置信度、状态、公司和期间。新增覆盖记录GET和解决请求POST。未授权/超范围案件或公司返回404，而非403。

- [ ] **步骤2：运行API测试并确认RED状态**

运行：`cd backend && pytest tests/integration/api/test_business_entertainment_api.py -q`

预期：由于路由/schema字段缺失，测试失败（FAIL）。

- [ ] **步骤3：实现轻量路由并完成注册**

路由只能调用应用服务。详情包含规范来源、可为空的SAP字段、金额来源、精确/模糊关联区分、引用片段、建议、缺失证据、制品/科目版本和合并历史。在阶段1 `main.py` 中注册路由。

- [ ] **步骤4：运行API测试并确认GREEN状态**

运行：`cd backend && pytest tests/integration/api/test_business_entertainment_api.py -q`

预期：测试通过（PASS），包括公司范围404和乐观锁409场景。

- [ ] **步骤5：编写UI的RED测试**

测试已关联/未关联及置信度过滤、“待定位SAP凭证”、证据引用、科目建议、仅允许精确关联的解决对话框、SAP覆盖语义和Excel导出操作。

- [ ] **步骤6：运行UI测试并确认RED状态**

运行：`npm --prefix web test -- --run src/features/risks/RiskPages.test.tsx src/features/business-entertainment/SapLinkCoveragePage.test.tsx`

预期：由于页面尚不存在，测试失败（FAIL）。

- [ ] **步骤7：实现UI并注册到阶段1 App**

使用TanStack Query和Ant Design。将源文本渲染为经过转义的文本节点。解决请求只提交已持久化的证据关联ID和行版本。合并后，使清单、来源/根案件详情、驾驶舱、导出元数据和KPI查询缓存失效。

- [ ] **步骤8：运行UI测试、lint和类型检查**

运行：`npm --prefix web test -- --run && npm --prefix web run lint && npm --prefix web run typecheck`

预期：测试通过（PASS），不存在失败测试、lint错误或类型错误。

- [ ] **步骤9：提交任务10**

~~~bash
git add backend/src/tax_risk/api backend/src/tax_risk/main.py backend/tests/integration/api web/src/features web/src/App.tsx
git commit -m "feat: expose entertainment review experience"
~~~

### 任务11：冻结双人复核黄金数据，并运行安全、指标和E2E发布门禁

**文件：**

- 新建：`backend/tests/fixtures/business_entertainment/golden.jsonl`
- 新建：`backend/tests/evaluation/test_business_entertainment_metrics.py`
- 新建：`backend/tests/evaluation/test_golden_governance.py`
- 新建：`backend/tests/security/test_prompt_injection.py`
- 新建：`backend/tests/e2e/test_business_entertainment_pipeline.py`
- 新建：`backend/tests/integration/application/test_business_entertainment_pipeline_wiring.py`
- 新建：`backend/tests/unit/api/test_business_entertainment_dependency_binding.py`
- 新建：`web/e2e/business-entertainment.spec.ts`
- 新建：`docs/runbooks/phase-2-business-entertainment-agent.md`
- 修改：`backend/src/tax_risk/application/business_entertainment/service.py`
- 修改：`backend/src/tax_risk/workers/business_entertainment.py`
- 修改：`backend/src/tax_risk/api/routes/business_entertainment.py`
- 修改：`backend/src/tax_risk/main.py`

- [ ] **步骤1：编写黄金数据治理的RED测试**

每条记录必须包含稳定样本ID、脱敏输入、来源模式、预期证据、财务标签/标注人/时间、税务标签/标注人/时间、不同的标注人、裁决人/最终标签、审批状态、冻结版本/校验和及冻结时间。只有 `APPROVED`+`FROZEN` 版本进入发布指标；冻结记录不可变。

- [ ] **步骤2：运行治理测试并确认RED状态**

运行：`cd backend && pytest tests/evaluation/test_golden_governance.py -q`

预期：在fixture校验器实现前测试失败（FAIL）。

- [ ] **步骤3：实现校验器并预置已裁决样例**

包括已关联的培训/会议用餐、有效接待、证据冲突、仅OA、合思+OA规范链、独立SAP覆盖，以及后续SAP解决。纳入未命中样本，避免召回率仅基于预警样本计算。

- [ ] **步骤4：运行治理测试并确认GREEN状态**

运行：`cd backend && pytest tests/evaluation/test_golden_governance.py -q`

预期：测试通过（PASS），校验和保持稳定。

- [ ] **步骤5：编写指标和安全性的RED测试**

针对每种来源模式分别测量候选召回率、模型召回率和证据复核后召回率。要求试点召回率≥90%、发布召回率≥95%、高置信度准确率≥80%，且已知案例零漏检。注入指令、PII和跨公司证据；断言权限/工具不发生变化、不留存PII且不存在未经授权的引用。

- [ ] **步骤6：运行指标/安全性测试并确认RED状态**

运行：`cd backend && pytest tests/evaluation/test_business_entertainment_metrics.py tests/security/test_prompt_injection.py tests/security/test_model_pii_retention_audit.py -q`

预期：在评估器和最终安全接线完成前测试失败（FAIL）。

- [ ] **步骤7：实现评估器和最终安全接线**

在CI中使用伪客户端确保确定性，输出机器可读指标，对未命中的负样本抽样，并在任一阈值或调用审计要求不满足时阻断发布。

- [ ] **步骤8：运行指标/安全性测试并确认GREEN状态**

运行：`cd backend && pytest tests/evaluation/test_business_entertainment_metrics.py tests/security/test_prompt_injection.py tests/security/test_model_pii_retention_audit.py -q`

预期：测试通过（PASS），满足四项必要阈值。

- [ ] **步骤9：编写后端E2E的RED测试**

覆盖已关联SAP链、未关联的合思+OA、仅OA风险/证据任务、独立SAP覆盖，以及精确关联合并后只有一笔活动金额。

- [ ] **步骤10：运行后端E2E并确认RED状态**

运行：`cd backend && pytest tests/e2e/test_business_entertainment_pipeline.py -q`

预期：在尚未接线的应用/API边界测试失败（FAIL）。

- [ ] **步骤11：完成应用服务流水线接线**

按照固定顺序为 `service.py` 接线：范围门禁 → 加载 `PUBLISHED` SnapshotSet → 精确关联 → 评估/覆盖 → 候选 → 受治理Agent → 共享/业务单据路由器。拒绝任何状态不是 `PUBLISHED` 或 `published_at` 为空的集合。不得引入另一套领域契约或重复报表查询。

- [ ] **步骤12：验证应用服务顺序**

运行：`cd backend && pytest tests/integration/application/test_business_entertainment_pipeline_wiring.py::test_service_orders_scope_snapshot_link_candidate_agent_and_router -q`

预期：测试通过（PASS）；各阶段按照锁定顺序调用一次，独立SAP在覆盖检查阶段停止。

- [ ] **步骤13：完成Celery任务参数接线**

修改 `workers/business_entertainment.py`，使每家公司任务只接收运行ID、公司、期间、`PUBLISHED` SnapshotSet ID，以及已发布的规则/模型/提示词/案例库/科目字典版本ID，随后在Worker进程内解析应用依赖。`PUBLISHED` 是SnapshotSet唯一可运行状态。

- [ ] **步骤14：验证Worker接线**

运行：`cd backend && pytest tests/integration/workers/test_business_entertainment_worker.py::test_worker_passes_snapshot_and_published_versions -q`

预期：测试通过（PASS）；重试时使用相同ID和幂等键。

- [ ] **步骤15：绑定与环境匹配的模型依赖**

新增FastAPI/应用依赖提供器：生产环境选择 `EnterpriseStructuredModelClient`，只有明确的测试配置才选择伪客户端；生产企业配置/零留存配置不完整时启动失败。

- [ ] **步骤16：验证依赖绑定**

运行：`cd backend && pytest tests/unit/api/test_business_entertainment_dependency_binding.py -q`

预期：测试通过（PASS）；生产环境绝不解析到伪客户端，无效企业配置以关闭方式失败。

- [ ] **步骤17：注册最终业务招待费路由**

在阶段1 `main.py` 中注册 `api/routes/business_entertainment.py`，只注入应用端口，并保留 `/api/v1` 和阶段1 Principal依赖。

- [ ] **步骤18：运行后端E2E并确认GREEN状态**

运行：`cd backend && pytest tests/e2e/test_business_entertainment_pipeline.py -q`

预期：五条路径和幂等重跑均测试通过（PASS）。

- [ ] **步骤19：新增并运行Playwright E2E**

运行：`npm --prefix web run test:e2e -- business-entertainment.spec.ts`

预期：过滤、证据详情、待定位、精确关联解决、合并后汇总和导出均测试通过（PASS）。

- [ ] **步骤20：编写运维手册**

记录版本发布、企业端点/零留存、schema失败队列、公司重跑、覆盖结果解读、黄金数据刷新、导出/KPI根案件规则和回滚。不得包含凭据或敏感源文本。

- [ ] **步骤21：运行完整阶段2验证**

运行：

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

预期：所有命令均通过（PASS）；迁移链为 `0006→0007→0008→0008a→0009→0010`，且只有一个Alembic迁移头；范围/导入/血缘/schema/安全/合并/聚合测试通过；发布指标达到已审批阈值。

- [ ] **步骤22：提交任务11**

~~~bash
git add backend/tests web/e2e/business-entertainment.spec.ts docs/runbooks/phase-2-business-entertainment-agent.md
git commit -m "test: gate business entertainment agent release"
~~~

## 最终完成定义

- [ ] 有效公司范围已版本化并经过独立复核，无效时阻断运行。
- [ ] SAP及四类前置来源通过阶段1 IngestBatch和不可变SnapshotSet血缘进入平台。
- [ ] 共享SAP观察和建议科目字典是阶段3复用的唯一契约。
- [ ] 精确关联、合思规范单据优先级、两种评估模式和独立SAP覆盖检查全部通过。
- [ ] 候选词库具有严格的版本化schema，已知正例候选零漏检。
- [ ] 模型判断不包含任何服务端权威字段；SemanticDetection在证据/科目/版本校验后组装。
- [ ] 企业调用要求使用已发布制品、执行PII最小化、确认零留存，并进行隐私安全的调用审计。
- [ ] 未关联业务单据可以创建SAP字段为空且状态为“待定位”的正式风险。
- [ ] 解决流程使用已持久化的精确证据ID，在服务端重新校验，并以事务方式合并。
- [ ] 合并和重试后，清单、驾驶舱、导出和KPI各自只统计一个根案件/一笔金额。
- [ ] 阶段4异步导出复用阶段2根案件行/schema生成器。
- [ ] API保留 `/api/v1/risk-cases`，超范围资源返回404。
- [ ] 黄金样本具有财务/税务双标签、裁决、审批、冻结和校验和。
- [ ] 试点召回率≥90%、发布召回率≥95%、高置信度准确率≥80%，已知样例零漏检。
- [ ] 未实现福利费或捐赠监测逻辑。

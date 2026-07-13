# 第四阶段治理、加固与推广实施计划

> **Agent 执行要求：** 必须使用 superpowers:subagent-driven-development（如有可用子 Agent）或 superpowers:executing-plans 实施本计划。各步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 对已完成的第一至第三阶段平台进行加固，使其能够在集团范围内受控投入生产使用，并具备统一身份模型、公司级隔离、不可变审计、安全导出、受保护的模型访问、可度量运维、签名发布和经过演练的回滚能力。

**架构：** 扩展现有模块化单体及其第一至第三阶段规范路径。通过 API 策略、PostgreSQL RLS 和语义证据读取器强制执行授权；审计仅允许追加；导出和模型调用必须重新应用服务端权限范围；可观测性按批次、公司和期间建立关联。生产晋级必须具备签名制品清单、历史重放、容量证据、UAT 和回滚证明。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy/Alembic、PostgreSQL RLS、Celery/Redis、OpenTelemetry、Prometheus 兼容指标、结构化 JSON 日志、React/TypeScript/Vite/Ant Design/TanStack Query、pytest/Hypothesis、Vitest/Playwright、Docker Compose、CI 工作流。

---

## 规范路径与迁移契约

本阶段扩展以下现有路径，不得创建相互竞争的身份模型或服务层：

```text
backend/src/tax_risk/
  security/principal.py                 # 阶段 1 的 Principal、角色、组织路径与公司权限范围
  api/dependencies.py                   # 阶段 1 的身份认证与数据库依赖
  domain/cases.py                       # 共享风险指纹与生命周期
  persistence/models.py                 # 共享 ORM 模型注册表
  persistence/repositories.py           # 共享的事务范围存储库
  application/master_data.py            # 阶段 1 的受控税务主数据用例
  application/semantic/model_client.py  # 阶段 2 的供应商中立 StructuredModelClient
  api/routes/cases.py                    # 共享风险清单、详情与操作路由
  api/routes/dashboard.py                # 共享 KPI 查询
  main.py                                # FastAPI 路由注册
  workers/celery_app.py                  # 共享 Celery 注册
web/src/App.tsx                          # 共享界面路由注册
```

第四阶段在第三阶段迁移头 `0003` 之后新增以下线性迁移：

```text
0004_company_isolation.py    down_revision = "0003"
0005_audit_hardening.py      down_revision = "0004"
0006_export_jobs.py          down_revision = "0005"
0007_release_manifests.py    down_revision = "0006"
```

## 工作块1：授权、审计、导出与模型安全

### 任务1：扩展统一 Principal，并在 API、PostgreSQL 和语义证据读取中强制执行公司权限范围

**文件：**
- 修改：`backend/src/tax_risk/security/principal.py`
- 新建：`backend/src/tax_risk/security/policies.py`
- 修改：`backend/src/tax_risk/api/dependencies.py`
- 修改：`backend/src/tax_risk/db.py`
- 新建：`backend/src/tax_risk/application/semantic/evidence_reader.py`
- 新建：`backend/migrations/versions/0004_company_isolation.py`
- 测试：`backend/tests/unit/security/test_policies.py`
- 测试：`backend/tests/integration/security/test_rls.py`
- 测试：`backend/tests/integration/security/test_rls_pool_reset.py`
- 测试：`backend/tests/integration/security/test_semantic_evidence_scope.py`

- [ ] **步骤1：编写预期失败的授权矩阵测试**

在不定义另一个 `Principal` 的前提下，测试以下已批准边界：

| 角色 | 允许 | 拒绝 |
|---|---|---|
| 集团税务 | 集团读取、复核、关闭、规则/模型/科目字典发布、主数据审批、按权限范围导出、审计读取 | 数据源接口管理 |
| 事业部/区域税务 | 读取已授权组织子树 | 复核、关闭、发布、主数据审批 |
| 公司财务 | 读取和处理本公司风险事项、登记调整凭证 | 访问其他公司、最终关闭、规则/模型发布 |
| 数据管理员 | 数据源/接口维护和主数据导入 | 税务风险结论、复核、关闭、规则/模型发布 |
| 审计人员 | 在已分配范围内读取版本、证据、风险事项和审计记录 | 除非另行授权，否则禁止任何写入或导出 |

`RUN_MONITOR` 对集团税务适用于全部公司，对明确委派的运维服务 Principal 则仅适用于一个已签名批次范围。API Principal 绝不继承工作进程权限。季度监测、业务招待费和共享月度语义工作进程分别使用独立服务身份，并受限于各自的队列、运行类型、批次、公司和期间。导出工作进程只能读取其任务已冻结的授权记录；模型网关只能读取当前单项评估已授权的证据引用。

操作枚举必须包含 `READ_RISK`、`PROCESS_COMPANY_RISK`、`CLOSE_RISK`、`RUN_MONITOR`、`MAINTAIN_SOURCE`、`IMPORT_MASTER`、`APPROVE_MASTER`、`MANAGE_RULE`、`PUBLISH_MODEL`、`EXPORT_RISK` 和 `READ_AUDIT`。

- [ ] **步骤2：运行策略测试并确认 RED**

运行：`cd backend && pytest tests/unit/security/test_policies.py -q`

预期：FAIL，因为完整的策略模块和操作尚不存在。

- [ ] **步骤3：通过扩展第一阶段身份模型实现策略**

保留第一阶段的 `subject`、`roles`、`allowed_company_ids` 和 `organization_path`。在服务端将组织后代节点解析为公司 ID；绝不信任客户端提供的权限范围。对于未授权的资源 ID 返回 404，对于列表中未授权的记录返回空集。工作进程调用使用受批次公司限制的已签名服务 Principal。

- [ ] **步骤4：编写预期失败的 RLS 和语义读取器测试**

覆盖 `ingest_batch`、`ingest_error`、公司 tax-master 记录、`accounting_snapshot`、源凭证/业务单据表、`detection_record`、`risk_case`、`review_action`、`evidence_task`、`evidence_link`、`sap_link_coverage`、`monthly_semantic_scope_fact`、`audit_event` 和语义证据投影。断言：

- 故意不加筛选的存储库查询不能返回其他公司的数据；
- 伪造的公司筛选条件不能扩大权限范围；
- `evidence.read_by_reference` 拒绝属于其他公司的引用；
- 集团税务可以查看全部公司，事业部/公司角色只能查看已分配公司；
- 连接归还连接池时，`SET LOCAL` 上下文已清除；
- 应用角色和工作进程角色不能绕过 RLS。

运行：`cd backend && pytest tests/integration/security/test_rls.py tests/integration/security/test_rls_pool_reset.py tests/integration/security/test_semantic_evidence_scope.py -q`

预期：FAIL，因为 RLS、连接上下文和按权限范围控制的证据读取器尚不存在。

- [ ] **步骤5：在 Alembic 中实现 RLS 和按权限范围控制的证据读取器**

迁移 `0004` 必须在每个公司范围表上启用并执行 `FORCE ROW LEVEL SECURITY`。直接归属公司的表将 `company_code` 与可信会话范围比较；子表通过关联其公司归属父表确定范围。每个事务内使用 `SET LOCAL` 设置 `app.subject`、`app.roles` 和 `app.company_scope`，并在连接归还连接池时重置。不得向应用角色授予表所有权或 `BYPASSRLS`。

V0.8 不部署外部向量索引或语义索引：语义候选和证据检索仍使用受相同 RLS 和 `EvidenceReader` 保护的 PostgreSQL 投影。CI 必须断言未配置外部语义索引端点，并运行跨公司证据检索测试。后续引入外部索引时，必须另行批准设计，强制执行公司命名空间/筛选条件，并具备对抗性隔离测试和回填/撤销控制。

`EvidenceReader.read_by_reference(principal, reference_id)` 执行策略授权和按范围控制的存储库读取。模型网关只能接收返回的证据，绝不能获得数据库句柄或自由 SQL 工具。

- [ ] **步骤6：运行全部授权测试和迁移检查**

运行：

```bash
cd backend
alembic upgrade 0004
pytest tests/unit/security tests/integration/security -q
alembic downgrade 0003 && alembic upgrade 0004
```

预期：PASS；跨公司的 API、SQL、连接池连接、工作进程和语义证据访问尝试均不能返回受保护数据；降级/升级退出码为 0。

- [ ] **步骤7：提交组织隔离变更**

```bash
git add backend/src/tax_risk/security backend/src/tax_risk/api/dependencies.py backend/src/tax_risk/db.py backend/src/tax_risk/application/semantic/evidence_reader.py backend/migrations/versions/0004_company_isolation.py backend/tests/unit/security backend/tests/integration/security
git commit -m "feat(auth): enforce company scope across data and evidence"
```

### 任务2：通过不可变审计确保敏感读写可追溯

**文件：**
- 修改：`backend/src/tax_risk/persistence/models.py`
- 新建：`backend/migrations/versions/0005_audit_hardening.py`
- 新建：`backend/src/tax_risk/application/audit.py`
- 新建：`backend/src/tax_risk/api/routes/audit.py`
- 修改：`backend/src/tax_risk/api/routes/cases.py`
- 修改：`backend/src/tax_risk/application/master_data.py`
- 修改：`backend/src/tax_risk/main.py`
- 测试：`backend/tests/unit/audit/test_audit_redaction.py`
- 测试：`backend/tests/integration/audit/test_append_only.py`
- 测试：`backend/tests/integration/audit/test_sensitive_actions.py`
- 测试：`backend/tests/integration/api/test_audit_routes.py`

- [ ] **步骤1：编写预期失败的审计覆盖测试**

在本任务边界内，登录/授权失败、风险清单查询、风险详情/证据查询、源数据上传、主数据导入/审批、现有规则/模型/科目字典发布，以及风险操作/最终关闭均必须生成事件。事件包含操作人、角色、公司范围、操作、目标、请求/批次 ID、规范化筛选条件哈希、返回行数、相关导出/查询 ID（如有）、变更前后摘要、结果、原因代码和 UTC 时间；自由文本证据和个人数据使用稳定引用或脱敏摘要表示。任务3将在相关功能实现后增加导出请求/下载事件，任务7增加发布/重放审批事件，任务8增加回滚事件。

- [ ] **步骤2：编写预期失败的数据库仅追加测试**

断言应用角色执行 `UPDATE` 和 `DELETE` 失败；插入操作不能覆盖 `occurred_at` 或操作人上下文；获授权审计人员只能查询已分配公司；读取审计端点不会递归创建无限审计循环。

运行：`cd backend && pytest tests/unit/audit tests/integration/audit tests/integration/api/test_audit_routes.py -q`

预期：FAIL，因为审计覆盖、脱敏、仅追加强制机制和路由尚不完整。

- [ ] **步骤3：扩展现有第一阶段审计模型和数据库控制**

不得创建第二张审计表。通过迁移 `0005` 为 `audit_event` 增加缺失的结构化字段、仅插入触发器、应用角色授权，以及时间、操作人、公司、操作和目标索引。由于被拒绝的业务事务会回滚，授权失败事件必须写入独立的安全审计事务。严禁改写已经执行的迁移 `0004`。

- [ ] **步骤4：增加统一审计应用服务和只读路由**

`application/audit.py` 负责脱敏、追加和按范围搜索。在步骤1所列服务/路由边界集成该服务。`GET /api/v1/audit-events` 要求 `READ_AUDIT` 权限，同时应用 API 权限范围和 RLS，执行分页，并排除未脱敏的请求正文。

- [ ] **步骤5：运行审计测试和迁移回归测试**

运行：`cd backend && alembic upgrade 0005 && pytest tests/unit/audit tests/integration/audit tests/integration/api/test_audit_routes.py -q && alembic current`

预期：PASS；截至本任务已实现的每项操作恰好生成一个主事件，禁止的变更操作失败，Alembic 报告 `0005`。在任务9之前，不得声称完整的后续操作矩阵已覆盖。

- [ ] **步骤6：提交不可变审计覆盖变更**

```bash
git add backend/src/tax_risk/persistence/models.py backend/migrations/versions/0005_audit_hardening.py backend/src/tax_risk/application/audit.py backend/src/tax_risk/api/routes/audit.py backend/src/tax_risk/api/routes/cases.py backend/src/tax_risk/application/master_data.py backend/src/tax_risk/main.py backend/tests/unit/audit backend/tests/integration/audit backend/tests/integration/api/test_audit_routes.py
git commit -m "feat(audit): preserve sensitive reads and decisions"
```

### 任务3：生成按权限范围控制的异步导出，并在下载时重新检查访问权限

**文件：**
- 新建：`backend/src/tax_risk/domain/exports.py`
- 新建：`backend/src/tax_risk/application/exports.py`
- 新建：`backend/src/tax_risk/workers/exports.py`
- 修改：`backend/src/tax_risk/application/business_entertainment/export.py`
- 修改：`backend/src/tax_risk/api/routes/exports.py`
- 修改：`backend/src/tax_risk/persistence/models.py`
- 修改：`backend/src/tax_risk/persistence/repositories.py`
- 修改：`backend/src/tax_risk/workers/celery_app.py`
- 修改：`backend/src/tax_risk/main.py`
- 新建：`backend/migrations/versions/0006_export_jobs.py`
- 新建：`web/src/features/exports/ExportJobsPage.tsx`
- 新建：`web/src/features/exports/ExportJobsPage.test.tsx`
- 修改：`web/src/App.tsx`
- 测试：`backend/tests/integration/exports/test_export_scope.py`
- 测试：`backend/tests/integration/exports/test_download_reauthorization.py`
- 测试：`backend/tests/integration/exports/test_export_audit.py`
- 测试：`backend/tests/unit/exports/test_spreadsheet_safety.py`

- [ ] **步骤1：编写预期失败的冻结范围和当前权限测试**

断言创建任务时取请求筛选条件与服务端权限范围的交集，工作进程使用冻结的授权范围；由于 RLS 始终生效，未限定范围的存储库调用不能扩大返回记录；下载时重新检查当前权限。生成后权限已被撤销的用户收到 404，且不能获得对象 URL。`test_export_audit.py` 要求创建、完成/失败、下载、拒绝和过期事件包含规范化筛选条件哈希、行数、校验和及导出任务 ID，但不得包含工作簿内容。

- [ ] **步骤2：编写预期失败的电子表格安全和生命周期测试**

以 `=`、`+`、`-` 或 `@` 开头的文本单元格必须添加单引号前缀；真实数值单元格（包括负数金额）必须保持数值类型。测试 queued/running/completed/failed/expired 状态、校验和、行数、模式版本、过期时间、对象键，以及过期后的对象删除/撤销。

运行：`cd backend && pytest tests/integration/exports tests/unit/exports -q`

预期：FAIL，因为导出领域、工作进程、迁移和安全策略尚不存在。

- [ ] **步骤3：实现迁移 `0006`、领域状态、存储库和用例**

存储请求操作人、角色/权限范围快照、规范化筛选条件、当前授权版本、模式版本、状态、行数、SHA-256、对象键、过期时间和失败代码。对象键由服务端生成。`create_export`、`render_export` 和 `authorize_download` 使用共享策略服务和审计服务。重构第二阶段同步业务招待费导出器，使其仅向通用任务服务提供记录/模式生成器；不得保留第二条授权或制品交付路径。

- [ ] **步骤4：注册工作进程和端点**

在 `workers/celery_app.py` 中注册导出任务；在 `main.py` 中注册创建/状态/下载路由。已完成的下载只有在重新验证当前权限和公司范围后，才返回短期有效 URL。绝不存储客户端提供的 URL。

- [ ] **步骤5：增加导出页面和组件测试**

页面展示状态、规范化范围、行数、校验和、过期时间和安全的失败原因。权限撤销或过期后隐藏下载入口，绝不暴露对象存储凭据。

运行：`cd web && npm test -- --run src/features/exports/ExportJobsPage.test.tsx && npm run build`

预期：PASS；组件测试和构建退出码为 0。

- [ ] **步骤6：运行后端导出测试和迁移连续性检查**

运行：`cd backend && alembic upgrade 0006 && pytest tests/integration/exports tests/unit/exports -q`

预期：PASS；权限已撤销的用户无法下载，不返回跨公司记录，文本公式注入已被中和，负数值仍保持数值类型。

- [ ] **步骤7：提交安全导出变更**

```bash
git add backend/src/tax_risk/domain/exports.py backend/src/tax_risk/application/exports.py backend/src/tax_risk/application/business_entertainment/export.py backend/src/tax_risk/workers backend/src/tax_risk/api/routes/exports.py backend/src/tax_risk/persistence backend/src/tax_risk/main.py backend/migrations/versions/0006_export_jobs.py backend/tests/integration/exports backend/tests/unit/exports web/src/features/exports web/src/App.tsx
git commit -m "feat(exports): scope and reauthorize risk downloads"
```

### 任务4：通过受保护的企业模型网关路由全部语义调用

**文件：**
- 新建：`backend/src/tax_risk/model_gateway/policy.py`
- 新建：`backend/src/tax_risk/model_gateway/service.py`
- 修改：`backend/src/tax_risk/application/semantic/model_client.py`
- 修改：`backend/src/tax_risk/application/semantic/evidence_reader.py`
- 修改：`backend/src/tax_risk/application/business_entertainment/agent.py`
- 修改：`backend/src/tax_risk/application/semantic/sap_voucher_agent.py`
- 修改：`backend/src/tax_risk/application/welfare/service.py`
- 修改：`backend/src/tax_risk/application/donation/service.py`
- 修改：`backend/src/tax_risk/adapters/model/enterprise_structured_client.py`
- 测试：`backend/tests/unit/model_gateway/test_payload_policy.py`
- 测试：`backend/tests/unit/model_gateway/test_structured_response.py`
- 测试：`backend/tests/unit/model_gateway/test_no_direct_adapter_imports.py`
- 测试：`backend/tests/integration/model_gateway/test_evidence_authorization.py`
- 测试：`backend/tests/security/test_prompt_injection.py`

- [ ] **步骤1：编写预期失败的数据最小化和供应商策略测试**

断言移除未列入允许清单的身份、电话、银行和附件字段；仅保留必要的事由、交易对手类型、参与人类别、场景、金额和引用片段。除非具备企业数据不用于公开训练的承诺和经批准的留存设置，否则拒绝生产模型供应商配置。

- [ ] **步骤2：编写预期失败的提示词注入、工具和公司范围测试**

将 OA/Hesi/SAP 文本中请求 SQL、新工具、其他公司、隐藏指令或模式变更的内容视为被引用证据。断言网关仅暴露 `evidence.read_by_reference`；证据读取器重新检查 Principal 和公司；模型输出不能更改规范身份、金额、来源模式、关联质量或公司。

运行：`cd backend && pytest tests/unit/model_gateway tests/integration/model_gateway tests/security/test_prompt_injection.py -q`

预期：FAIL，因为受保护的网关策略尚不存在。

- [ ] **步骤3：实现策略、网关和严格响应组装**

网关接收服务端持有的上下文和第二阶段 `StructuredModelClient`；准备经过允许清单筛选的载荷，记录供应商/模型/提示词/案例库版本，调用适配器，验证仅含模型判断的模式，并由服务端组装最终检测结果。拒绝未列入允许清单的工具请求和模式校验失败。第二次模式校验失败时创建技术复核事项，而不是安全结果或税务风险。

- [ ] **步骤4：移除直接供应商调用路径并增加可审计元数据**

业务招待费以及福利费/捐赠支出共享的 SAP 凭证 Agent 通过网关调用；其服务工厂不得直接注入企业适配器。增加基于 AST 的架构测试：任何 `model_gateway/service.py` 之外的模块导入或构造 `EnterpriseStructuredClient` 时测试失败。记录模型适配器、版本 ID、词元数量、延迟、策略结果、请求哈希和错误代码，但不记录完整自由文本。审计/日志记录中不得存储公开训练授权或供应商凭据。

- [ ] **步骤5：运行模型及累计语义测试套件**

运行：

```bash
cd backend
pytest tests/unit/model_gateway tests/integration/model_gateway tests/security/test_prompt_injection.py -q
pytest tests/unit/business_entertainment tests/unit/semantic tests/evaluation -q
```

预期：PASS；恶意文本不能扩展工具或公司范围，服务端持有的字段保持不变，三项监测的金标数据集全部保持通过。

- [ ] **步骤6：提交模型网关控制变更**

```bash
git add backend/src/tax_risk/model_gateway backend/src/tax_risk/application/semantic backend/src/tax_risk/application/business_entertainment/agent.py backend/src/tax_risk/application/welfare/service.py backend/src/tax_risk/application/donation/service.py backend/src/tax_risk/adapters/model/enterprise_structured_client.py backend/tests/unit/model_gateway backend/tests/integration/model_gateway backend/tests/security/test_prompt_injection.py
git commit -m "feat(ai-security): constrain enterprise model data and tools"
```

## 工作块2：运维、发布证据与可逆推广

### 任务5：增加关联日志、指标、链路追踪、健康检查和运维视图

**文件：**
- 新建：`backend/src/tax_risk/observability/context.py`
- 新建：`backend/src/tax_risk/observability/metrics.py`
- 新建：`backend/src/tax_risk/observability/tracing.py`
- 修改：`backend/src/tax_risk/api/routes/health.py`
- 修改：`backend/src/tax_risk/main.py`
- 修改：`backend/src/tax_risk/workers/celery_app.py`
- 新建：`infra/observability/otel-collector.yaml`
- 新建：`infra/observability/dashboard.json`
- 新建：`web/src/features/operations/OperationsDashboard.tsx`
- 新建：`web/src/features/operations/OperationsDashboard.test.tsx`
- 修改：`web/src/App.tsx`
- 测试：`backend/tests/unit/observability/test_context.py`
- 测试：`backend/tests/integration/observability/test_health.py`
- 测试：`backend/tests/integration/observability/test_metrics.py`

- [ ] **步骤1：编写预期失败的关联和指标测试**

每条 API/工作进程日志和链路必须携带请求 ID 或任务 ID，并在可用时携带批次、公司、会计年度和期间。指标覆盖数据源就绪情况、质量阻断、公司任务结果、公式运行时间、语义候选/检测/错误、关联覆盖率、证据积压、风险事项账龄、导出、授权失败、数据就绪时间和输出就绪时间。禁止将公司名称和自由文本作为指标标签。

- [ ] **步骤2：编写预期失败的存活/就绪测试**

存活检查仅检查进程响应能力。就绪检查覆盖 PostgreSQL、Redis、对象存储、已配置的预期迁移头、有效规则/版本清单和模型网关配置，但不调用外部模型。本任务期间的预期迁移头为 `0006`；任务7将生产配置和测试调整为 `0007`。依赖项失败时返回 503 和稳定的组件代码。

运行：`cd backend && pytest tests/unit/observability tests/integration/observability -q`

预期：FAIL，因为上下文传播、指标和就绪检查尚不存在。

- [ ] **步骤3：实现遥测和依赖项健康检查**

通过 FastAPI 中间件和 Celery 请求头传播上下文。导出结构化 JSON 日志、链路、计数器和直方图。将 `data_ready_at` 定义为不可变的 `SnapshotSet.published_at`，在所有必需数据源成员通过质量门禁时写入。所有公司任务达到任一终态时持久化 `batch_finished_at`；仅当有效公司的检测、覆盖/证据任务和风险以 `SUCCEEDED` 状态提交后，才持久化 `company_output_ready_at`。即使批次为 `PARTIAL_SUCCESS`，技术失败公司的 `company_output_ready_at` 仍为空；只有全部有效公司均成功时，批次级 `output_ready_at` 才取公司时间戳最大值。第一阶段负责 `SnapshotSet.published_at`；第二、三阶段绝不能以上传时间或模型调用时间替代。

- [ ] **步骤4：增加运维驾驶舱**

分别展示数据错误、技术失败和税务风险。包括批次/公司状态、队列等待时长、交付延迟倒计时、供应商失败、关联覆盖率、证据积压，以及受 `RUN_MONITOR` 权限限制的重试控制。

运行：`cd web && npm test -- --run src/features/operations/OperationsDashboard.test.tsx && npm run build`

预期：PASS；运维界面能区分三类问题，图表中不显示敏感自由文本。

- [ ] **步骤5：运行遥测和路由回归测试**

运行：`cd backend && pytest tests/unit/observability tests/integration/observability tests/integration/api -q`

预期：PASS；上下文能够跨越 API 到工作进程的边界，就绪检查绝不调用模型，依赖项代码保持稳定。

- [ ] **步骤6：提交运维可视化变更**

```bash
git add backend/src/tax_risk/observability backend/src/tax_risk/api/routes/health.py backend/src/tax_risk/main.py backend/src/tax_risk/workers/celery_app.py backend/tests/unit/observability backend/tests/integration/observability infra/observability web/src/features/operations web/src/App.tsx
git commit -m "feat(ops): expose monitored batch health"
```

### 任务6：证明季度及全部月度监测能够隔离失败、安全重跑，并满足100家以上公司的处理时限

**文件：**
- 新建：`backend/src/tax_risk/domain/task_runs.py`
- 修改：`backend/src/tax_risk/workers/quarterly_batch.py`
- 修改：`backend/src/tax_risk/workers/business_entertainment.py`
- 修改：`backend/src/tax_risk/workers/monthly_semantic.py`
- 新建：`backend/tests/unit/workers/test_task_run_contract.py`
- 新建：`backend/tests/load/profiles/126_companies.json`
- 新建：`backend/tests/load/conftest.py`
- 新建：`backend/tests/load/test_capacity_profile.py`
- 新建：`backend/tests/load/test_failure_isolation.py`
- 新建：`backend/tests/load/test_replay_idempotency.py`
- 新建：`backend/tests/load/test_t_plus_2.py`

- [ ] **步骤1：定义并验证固定验收配置**

该配置包含126家公司、每家公司一份季度快照、每家公司在三项监测中共 1,000 条月度 SAP/OA/Hesi 明细、16 个工作进程、一个强制数据源失败、一个可重试的供应商失败，以及一个不可重试的主数据错误。**有效公司**是指在 `SnapshotSet.published_at` 时点，其所需数据源成员、适用的受控公司清单和已批准主数据/版本输入均完整的公司；数据源和主数据阻断应单独报告，并从有效公司成功率和 T+2 分母中排除，绝不能计为安全。供应商失败发生在有效输入之后，因此保留在两个分母中。报告记录公司总数、有效数、阻断数、技术失败数和成功数，以及 CPU/内存配置、并发度、时间戳、任务数、行数、重试次数和队列最长等待时长。

- [ ] **步骤2：编写预期失败的隔离和幂等性测试**

首先编写 `test_task_run_contract.py`。`TaskRunResult` 包含运行类型、监测类型、批次、公司、期间、幂等键、终态、重试次数、时间戳和稳定错误代码。锁定以下键：

- 季度监测：`company|fiscal_year|quarter|snapshot_set|rule_version`；
- 业务招待费：`company|fiscal_year|through_month|snapshot_set|company_list|rule|model|prompt|case_library|account_dictionary`；
- 福利费或捐赠支出：`company|fiscal_year|through_month|monitor_type|snapshot_set|rule|model|prompt|case_library|account_dictionary`。

断言有效公司成功率至少为 98%；单家公司失败绝不回滚其他公司；只有可重试失败才执行有界指数退避重试；失败公司可以单独重跑。重放相同键输入时，每个指纹保留一个风险事项，每个键保留一个候选/证据任务，业务单据与 SAP 合并后仅保留一个有效金额。变更任一受控版本会创建独立运行，但不改变稳定风险指纹。

- [ ] **步骤3：编写预期失败的容量和 T+2 测试**

在文档规定的 8-vCPU/16-GB 参考运行器上，以并发度 16 运行时，126 家公司配置必须在 24 小时内完成。每家有效公司必须在其已持久化的数据就绪时间戳之后 48 小时内达到 `SUCCEEDED` 并获得 `company_output_ready_at`；即使总体成功率仍不低于 98%，供应商失败如未在截止时间前成功恢复，也会导致 T+2 门禁失败。使用合成时钟独立于实际测试耗时验证精确的 48 小时边界。

运行：`cd backend && pytest tests/load/test_failure_isolation.py tests/load/test_replay_idempotency.py tests/load/test_t_plus_2.py -q`

预期：FAIL，直至所有工作进程路径均使用稳定幂等键、隔离事务、有界重试和交付时间戳。

- [ ] **步骤4：实现共享任务结果和有界重试行为**

在 `domain/task_runs.py` 中实现共享结果信封和精确键，并在全部四条监测任务路径中使用。福利费和捐赠支出共享 `workers/monthly_semantic.py`，但保留不同的 `monitor_type` 键和结果。可重试技术错误使用设有上限的指数退避；数据源/主数据/业务错误直接阻断，不自动重试。在汇入聚合前持久化各公司结果，并允许仅重跑单家公司。`backend/tests/load/conftest.py` 必须注册 `--capacity-report`、验证其父目录，并且即使门禁失败也要写入文档规定的 JSON 模式。

- [ ] **步骤5：运行完整配置并写入容量制品**

运行：`cd backend && pytest tests/load -q --capacity-report=../artifacts/acceptance/phase-4/capacity-report.json`

预期：PASS；成功率至少为 98%，强制失败已隔离，有效风险敞口重复数为零，参考配置耗时不超过 24 小时，每家有效公司均在 48 小时内获得成功输出，失败/部分成功任务绝不会获得虚假的输出就绪时间戳。

- [ ] **步骤6：提交韧性和容量证明**

```bash
git add backend/src/tax_risk/domain/task_runs.py backend/src/tax_risk/workers backend/tests/unit/workers/test_task_run_contract.py backend/tests/load
git commit -m "test(ops): prove group batch resilience and timeliness"
```

### 任务7：构建签名制品清单、重放门禁、CI 和可执行验证目标

**文件：**
- 新建：`backend/src/tax_risk/release/manifest.py`
- 新建：`backend/src/tax_risk/release/signing.py`
- 新建：`backend/src/tax_risk/release/replay_runner.py`
- 新建：`backend/src/tax_risk/release/replay_gate.py`
- 新建：`backend/src/tax_risk/release/reporting.py`
- 新建：`backend/src/tax_risk/adapters/signing/kms_ed25519_signer.py`
- 修改：`backend/src/tax_risk/persistence/models.py`
- 新建：`backend/migrations/versions/0007_release_manifests.py`
- 新建：`backend/tests/unit/release/test_manifest.py`
- 新建：`backend/tests/unit/release/test_signature.py`
- 新建：`backend/tests/integration/release/test_kms_signer.py`
- 新建：`backend/tests/integration/release/test_replay_gate.py`
- 新建：`backend/tests/integration/release/test_release_audit.py`
- 新建：`Makefile`
- 修改：`web/playwright.config.ts`
- 新建：`infra/scripts/verify_governance.sh`
- 新建：`infra/scripts/verify_release.sh`
- 新建：`infra/scripts/verify_capacity.sh`
- 新建：`infra/scripts/verify_migrations.sh`
- 新建：`infra/scripts/security_check.sh`
- 新建：`infra/scripts/run_uat.sh`
- 新建：`.github/workflows/ci.yml`
- 新建：`.github/workflows/release.yml`
- 新建：`infra/runbooks/release.md`

- [ ] **步骤1：编写预期失败的规范清单和签名测试**

规范 JSON 清单包含应用镜像摘要、Git 提交标识、迁移头、规则包、提示词包、模型适配器/配置版本、科目字典、案例库和评估/重放报告哈希。使用已批准公钥验证 Ed25519 签名；变更任一字节、制品哈希或迁移头都必须导致验证失败。生产签名通过工作负载身份、环境密钥 ID 允许清单和可审计签名操作调用已批准的 KMS/HSM；私钥材料绝不进入进程。测试使用临时密钥和模拟 KMS 端点。

- [ ] **步骤2：编写预期失败的确定性和语义重放门禁测试**

除非公式/基准真值准确率为 100%、可追溯率为 100%、主数据缺陷阻断率为 100%、已知语义案例漏检数为零、试点召回率至少为 90%、正式生产召回率至少为 95%、高置信度准确率至少为 80%、集团批次成功率至少为 98%，且安全/迁移/回滚检查通过，否则必须阻断发布。`test_release_audit.py` 要求候选版本创建、重放开始/结果、批准/拒绝、签名、验证和晋级事件包含清单/报告哈希及批准人身份。

运行：`cd backend && pytest tests/unit/release tests/integration/release -q`

预期：FAIL，因为清单、签名、持久化和重放门禁尚不存在。

- [ ] **步骤3：实现单一职责发布模块、KMS 签名和迁移 `0007`**

`manifest.py` 执行规范化和哈希；`signing.py` 定义签名器/验证器端口及公钥验证器；`replay_runner.py` 运行冻结快照；`replay_gate.py` 评估阈值；`reporting.py` 写入 JSON 和人类可读报告。`kms_ed25519_signer.py` 获取短期工作负载令牌，验证所请求密钥 ID 已列入允许清单，请求 KMS/HSM 对规范摘要签名，并返回签名及密钥/版本 ID。在文档规定的密钥轮换重叠期内，验证程序信任当前有效公钥和明确保留的上一公钥；未知或已退役 ID 必须拒绝。持久化清单哈希、签名、签名方密钥/版本 ID、制品引用、批准记录和重放报告，但不存储私钥。

- [ ] **步骤4：使用前增加可执行 Make 目标**

创建以下精确目标映射；每个脚本均使用 `set -euo pipefail`，创建输出目录，验证生成的 JSON/JUnit 文件，并在门禁失败时以非零状态退出：

| Make 目标 | 纳入版本控制的命令 | 必需制品 |
|---|---|---|
| `test-backend` | `cd backend && pytest --junitxml=../artifacts/acceptance/backend.xml` | `backend.xml` |
| `test-web` | `cd web && npm test -- --run && npm run build && PLAYWRIGHT_JSON_OUTPUT_NAME=../artifacts/acceptance/web-test-results/results.json npx playwright test --reporter=json` | `artifacts/acceptance/web-test-results/results.json` |
| `verify-governance` | `infra/scripts/verify_governance.sh` | `phase-4/governance.xml` |
| `verify-release` | `infra/scripts/verify_release.sh` | `phase-4/replay-report.json` 和已签名清单 |
| `verify-capacity` | `infra/scripts/verify_capacity.sh` | `phase-4/capacity-report.json` |
| `verify-migrations` | `infra/scripts/verify_migrations.sh` | `phase-4/migrations.json` |
| `security-check` | `infra/scripts/security_check.sh` | `phase-4/security.json` |
| `uat` | `infra/scripts/run_uat.sh` | `phase-4/uat-scorecard.json` |
| `verify-rollback` | 任务8中的 `infra/scripts/rollback_drill.sh` | `phase-4/rollback-report.json` |

- [ ] **步骤5：增加 CI 和签名发布工作流**

CI 运行后端、前端、类型/代码检查、依赖/安全检查、从空库迁移、从 `0003` 迁移、RLS、PostgreSQL 语义证据检索隔离、无外部索引配置断言及 E2E 测试。发布流程在重放前验证候选清单，仅在批准后签名，上传清单/签名/报告，并在晋级前重新验证已下载制品。

- [ ] **步骤6：运行发布测试和目标冒烟检查**

运行：

```bash
cd backend && alembic upgrade 0007 && pytest tests/unit/release tests/integration/release -q
cd .. && make verify-governance && make verify-release
```

预期：PASS；篡改测试以拒绝方式失败；有效清单验证通过；重放报告写入 `artifacts/acceptance/phase-4/replay-report.json`。

- [ ] **步骤7：提交签名发布门禁变更**

```bash
git add backend/src/tax_risk/release backend/src/tax_risk/adapters/signing backend/src/tax_risk/persistence/models.py backend/migrations/versions/0007_release_manifests.py backend/tests/unit/release backend/tests/integration/release Makefile web/playwright.config.ts .github/workflows infra/scripts/verify_governance.sh infra/scripts/verify_release.sh infra/scripts/verify_capacity.sh infra/scripts/verify_migrations.sh infra/scripts/security_check.sh infra/scripts/run_uat.sh infra/runbooks/release.md
git commit -m "ci: gate releases on signed replay evidence"
```

### 任务8：自动化回滚演练、试点验收和分批推广

**文件：**
- 新建：`backend/src/tax_risk/release/scorecard.py`
- 新建：`backend/tests/unit/release/test_scorecard.py`
- 新建：`backend/tests/integration/release/test_rollback_drill.py`
- 新建：`backend/tests/integration/release/test_rollback_audit.py`
- 新建：`infra/scripts/rollback_drill.sh`
- 新建：`infra/runbooks/rollback.md`
- 新建：`infra/runbooks/data-source-failure.md`
- 新建：`infra/runbooks/model-provider-failure.md`
- 新建：`infra/runbooks/pilot-uat.md`
- 新建：`infra/runbooks/group-rollout.md`
- 新建：`docs/operations/acceptance-scorecard.md`
- 新建：`docs/operations/data-owner-checklist.md`
- 新建：`docs/operations/user-training.md`

- [ ] **步骤1：编写预期失败的生产评分卡测试**

公式准确率 100%、可追溯率 100%、主数据阻断率 100%、有效公司成功率至少 98%、正式召回率至少 95%、高置信度准确率至少 80%、已知案例漏检数为零、月度交付不超过 48 小时、授权/RLS/语义证据检索隔离、无外部索引配置、审计不可变性、已验证签名、恢复和回滚，均必须具备证据引用。缺少任何证据时，`production_ready` 为 false。

- [ ] **步骤2：编写预期失败的回滚故障注入、幂等性和续执行测试**

注入无效模型配置、携带执行中任务但被终止的工作进程、已完成导出但权限被撤销的用户，以及不兼容的候选制品。断言演练会排空或安全撤销任务，记录受影响批次 ID，撤销下载，选择上一份已验证清单，仅在一次性恢复副本上执行恢复/降级，重新部署，验证数据源/快照/风险校验和，并重跑一家代表性公司且不产生重复风险敞口。使用相同已批准输入执行两次，断言不会创建第二次恢复、撤销、部署或风险事项。在每个阶段后注入失败，再从持久化检查点继续，并证明已完成阶段经验证后跳过，其余阶段执行完成。`test_rollback_audit.py` 要求请求/批准、每次检查点转换、失败/恢复、清单切换、校验和结果、代表性重跑和恢复决策事件。

运行：`cd backend && pytest tests/unit/release/test_scorecard.py tests/integration/release/test_rollback_drill.py -q`

预期：FAIL，因为评分卡和可重复回滚演练尚不存在。

- [ ] **步骤3：实现评分卡和精确回滚脚本**

`rollback_drill.sh` 接收候选清单、上一清单、备份 ID、受影响批次 ID、环境、已批准变更 ID 和检查点路径。脚本执行预检签名验证、任务排空/撤销、导出撤销、将备份恢复至隔离目标、可选的已测试迁移降级、上一应用/制品部署、校验和比较、代表性重跑和 JSON 报告生成。每个阶段在推进前写入幂等键、输入哈希、终态和证据；重新运行时验证已完成证据，并从第一个未完成阶段继续。每项破坏性生产操作均要求已批准变更 ID 和环境防护。

- [ ] **步骤4：编写包含命令和负责人的运维手册**

每份运维手册均明确告警信号、决策负责人、批准要求、遏制命令、回滚命令、数据一致性检查、沟通方式、恢复证明和制品路径。模型失败时保留候选项供后续判断，绝不标记为安全。数据源失败时创建数据异常，绝不输出“无风险”。

- [ ] **步骤5：在冻结快照上执行试点 UAT**

试点顺序：内部测试公司、覆盖盈利/亏损及关联模式的选定公司、一次完整季度并行运行，然后按组织分批推广。财务和税务双重复核标准公式及抽样语义案例；数据负责人签字确认对账结果；安全/运维负责人签字确认隔离、恢复和回滚证据。

运行：`make verify-rollback && make uat SNAPSHOT_SET=pilot-2026q2`

预期：PASS；`rollback-report.json` 包含 `recovery_verified=true`；`uat-scorecard.json` 记录全部阈值和批准人，并且仅当所有门禁通过时才包含 `production_ready=true`。

- [ ] **步骤6：提交推广控制变更**

```bash
git add backend/src/tax_risk/release/scorecard.py backend/tests/unit/release/test_scorecard.py backend/tests/integration/release/test_rollback_drill.py infra/scripts infra/runbooks docs/operations
git commit -m "docs(rollout): make tax monitoring release reversible"
```

### 任务9：运行并记录最终全系统验证

**文件：**
- 修改：`README.md`
- 修改：`docs/operations/acceptance-scorecard.md`
- 新建：`backend/tests/integration/audit/test_full_action_matrix.py`

- [ ] **步骤1：验证完整审计操作矩阵**

运行：`cd backend && pytest tests/integration/audit/test_full_action_matrix.py -q`

预期：每项敏感读写及导出、发布/重放和回滚操作均生成所需脱敏事件；矩阵所有行均已覆盖，不得使用桩代码将后续操作标记为已覆盖。

- [ ] **步骤2：验证后端、前端和端到端行为**

运行：`make test-backend && make test-web`

预期：所有 pytest、Vitest、构建和 Playwright 检查退出码均为 0。

- [ ] **步骤3：验证授权、安全和迁移**

运行：`make verify-governance && make security-check && make verify-migrations`

预期：RLS/API/语义证据对抗性测试及无外部索引断言通过；不存在未解决的高严重级别依赖或静态检查发现；空数据库和 `0003` 数据库均成功升级至 `0007`；文档规定的一次性降级成功。

- [ ] **步骤4：验证重放、容量、及时性和回滚**

运行：`make verify-release && make verify-capacity COMPANY_FIXTURE=126 && make verify-rollback`

预期：签名和重放门禁获批；有效公司成功率至少为 98%；容量配置在 24 小时内完成；月度交付在 48 小时内完成；回滚恢复已验证。

- [ ] **步骤5：验证试点证据和清单完整性**

运行：`make uat SNAPSHOT_SET=pilot-2026q2`

预期：公式准确率、可追溯率和主数据阻断率均为 100%；正式召回率至少为 95%；高置信度准确率至少为 80%；已知案例漏检数为零；`production_ready=true`。

- [ ] **步骤6：记录证据链接并提交交接变更**

在 README 中补充启动、监控、备份、恢复、发布、回滚和升级处理命令。在评分卡中链接每项验收制品和批准记录。

```bash
git add README.md docs/operations/acceptance-scorecard.md backend/tests/integration/audit/test_full_action_matrix.py artifacts/acceptance
git commit -m "docs: hand off verified tax monitoring operations"
```

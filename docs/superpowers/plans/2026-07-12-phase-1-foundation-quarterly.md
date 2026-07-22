# 阶段 1 基础底座与季度监测实施计划

> **面向 Agent 工作单元：** 必须使用 superpowers:subagent-driven-development（如可使用子 Agent）或 superpowers:executing-plans 来实施本计划。各步骤使用复选框（`- [ ]`）语法跟踪。

**目标：** 构建具备生产形态的阶段 1 基础底座，采集 100 多家公司的受控季度数据，发布通过质量门禁的不可变快照，执行三项已批准的确定性税务计算，创建可审计的风险事项，并提供最小可用的季度驾驶舱。

**架构：** 采用模块化 Python 服务，包含纯领域计算、SQLAlchemy 仓储、FastAPI API 以及按公司分片的 Celery 批处理工作任务。PostgreSQL 是控制面、数据血缘、计算结果及风险事项的唯一事实来源；Redis 承载持久化任务协调；React 调用只读季度 API。阶段 1 不包含任何 LLM、嵌入、提示词、向量存储或语义 Agent 代码。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2/Alembic、PostgreSQL 16、Celery 5/Redis 7、pytest/Hypothesis；React 18、TypeScript、Vite、Ant Design、TanStack Query、Vitest/Testing Library；Docker Compose 用于本地基础设施。

---

**源规格说明书：** docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md

**执行规则：** 每项行为均遵循 @superpowers:test-driven-development，每个工作块移交前均遵循 @superpowers:verification-before-completion，并且只有在指定的聚焦检查通过后才能提交。除非命令以 cd 开头，否则均假定从仓库根目录执行。

## 计划文件清单

### 后端基础底座

- **backend/pyproject.toml** — Python 包、运行时及测试依赖、pytest/覆盖率配置。
- **backend/src/tax_risk/config.py** — 基于环境变量的配置。
- **backend/src/tax_risk/db.py** — SQLAlchemy 引擎、会话工厂及声明式基类。
- **backend/src/tax_risk/main.py** — FastAPI 应用工厂。
- **backend/src/tax_risk/domain/money.py** — Money、Rate 及 ROUND_HALF_UP 契约。
- **backend/src/tax_risk/domain/quarterly.py** — 纯季度输入、结果及公式。
- **backend/src/tax_risk/domain/cases.py** — 风险指纹及状态流转策略。
- **backend/src/tax_risk/persistence/models.py** — 规范 SQLAlchemy Base 及聚焦模型导入登记；后续阶段继续使用此规范路径。
- **backend/src/tax_risk/persistence/repositories.py** — 规范的事务范围 UnitOfWork 及聚焦仓储组合；后续阶段继续使用此规范路径。
- **backend/src/tax_risk/persistence/ingest_models.py** — Company、IngestBatch、IngestError 及 SourceRecord 表。
- **backend/src/tax_risk/persistence/master_models.py** — 带生效日期的税务主数据表及规则版本表。
- **backend/src/tax_risk/persistence/snapshot_models.py** — AccountingSnapshot、SnapshotSource、SnapshotSet 及 SnapshotSetMember 表。
- **backend/src/tax_risk/persistence/risk_models.py** — MonitoringRun、DetectionRecord、RiskCase、ReviewAction 及 AuditEvent 表。
- **backend/src/tax_risk/persistence/ingest_repositories.py** — 公司及数据采集持久化操作。
- **backend/src/tax_risk/persistence/master_repositories.py** — 税务主数据及规则版本操作。
- **backend/src/tax_risk/persistence/snapshot_repositories.py** — 质量门禁、锁定及快照发布操作。
- **backend/src/tax_risk/persistence/risk_repositories.py** — 运行、监测结果、风险事项、复核及审计操作。
- **backend/src/tax_risk/application/ingest.py** — IngestBatch 用例。
- **backend/src/tax_risk/application/companies.py** — SAP 公司基础信息导入及有效公司查询。
- **backend/src/tax_risk/application/master_data.py** — 版本化税务主数据导入及查询。
- **backend/src/tax_risk/application/snapshots.py** — 质量门禁及不可变快照发布。
- **backend/src/tax_risk/application/quarterly_runs.py** — 公式执行、监测结果及风险事项。
- **backend/src/tax_risk/adapters/ingest/base.py** — 规范批量适配器协议。
- **backend/src/tax_risk/adapters/ingest/csv_adapter.py** — 参考 CSV 适配器。
- **backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py** — 受控 XLSX 主数据适配器。
- **backend/src/tax_risk/api/schemas.py** — Pydantic v2 传输模式。
- **backend/src/tax_risk/api/dependencies.py** — 会话及主体范围。
- **backend/src/tax_risk/api/routes/** — 健康检查、数据采集、主数据、快照、运行、风险事项及驾驶舱。
- **backend/src/tax_risk/workers/celery_app.py** — Celery 配置及路由。
- **backend/src/tax_risk/workers/quarterly_batch.py** — 扇出、公司任务及扇入汇总。
- **backend/migrations/** — Alembic 环境及编号模式迁移。
- **backend/tests/unit/** — 纯领域及应用测试。
- **backend/tests/integration/** — PostgreSQL/API/Celery eager 模式测试。
- **backend/tests/e2e/** — 标准数据的完整季度验收。

### 前端与基础设施

- **web/src/api/client.ts** — 类型化 HTTP 客户端。
- **web/src/api/quarterly.ts** — 驾驶舱、运行及风险事项查询。
- **web/src/features/quarterly/types.ts** — 界面契约。
- **web/src/features/quarterly/QuarterlyDashboardPage.tsx** — 最小可用驾驶舱。
- **web/src/features/quarterly/QuarterlyRunTable.tsx** — 公司状态及风险表。
- **web/src/features/quarterly/FormulaDrawer.tsx** — 计算代入过程及数据血缘。
- **web/src/App.tsx** — 路由及查询提供器。
- **web/src/**/*.test.tsx** — 组件测试。
- **web/e2e/quarterly-dashboard.spec.ts** — 浏览器验收。
- **infra/docker-compose.yml** — PostgreSQL、Redis、API、工作任务及 Web。
- **infra/env.example** — 不含密钥的本地配置。
- **infra/README.md** — 启动、迁移、数据准备、运行及验证命令。

## 工作块 1：基础底座、数据契约与不可变快照

### 任务 1：初始化全新后端与前端

**文件：**
- 新建：**backend/pyproject.toml**
- 新建：**backend/src/tax_risk/__init__.py**
- 新建：**backend/src/tax_risk/config.py**
- 新建：**backend/src/tax_risk/db.py**
- 新建：**backend/src/tax_risk/main.py**
- 新建：**backend/src/tax_risk/api/routes/health.py**
- 新建：**backend/tests/unit/api/test_health.py**
- 新建：**web/package.json**
- 新建：**web/tsconfig.json**
- 新建：**web/vite.config.ts**
- 新建：**web/index.html**
- 新建：**web/src/main.tsx**
- 新建：**web/src/App.tsx**
- 新建：**web/src/App.test.tsx**
- 新建：**infra/docker-compose.yml**
- 新建：**infra/env.example**
- 新建：**.gitignore**

- [ ] **步骤 1：添加包清单及预期失败的健康检查测试**

使用 Python 3.12，并在 **backend/pyproject.toml** 中声明 FastAPI、Pydantic Settings、SQLAlchemy、Alembic、psycopg、Celery、Redis、python-multipart、openpyxl、pytest、pytest-cov、Hypothesis、httpx、Ruff 及 mypy。为 pytest 配置 pythonpath=src 及严格标记。添加以下后端测试：

~~~python
from fastapi.testclient import TestClient

from tax_risk.main import create_app


def test_health_reports_service_ready() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tax-risk"}
~~~

配置 React/Vite/TypeScript、Ant Design、@tanstack/react-query、Vitest、Testing Library、Playwright、ESLint 及 Prettier。添加 App 测试，要求标题为“集团所得税风险监测”。

- [ ] **步骤 2：运行测试并确认红灯状态**

运行：cd backend && python3.12 -m pip install -e '.[dev]' && pytest tests/unit/api/test_health.py -q

预期：失败，因为 tax_risk.main 或 create_app 尚不存在。

运行：cd web && npm install && npm test -- --run

预期：失败，因为 App 尚未渲染要求的标题。

- [ ] **步骤 3：实现最小应用外壳**

创建包含 database_url、redis_url、environment 及 development_principal_enabled 的 Settings 类。将 create_app 实现为应用工厂，加入 GET /health 路由，暂不暴露业务端点。使用 Ant Design Layout 构建 App 并展示要求的标题。在 **infra/docker-compose.yml** 中配置 PostgreSQL 16 和 Redis 7 健康检查；不得添加语义模型服务。

- [ ] **步骤 4：确认绿灯状态并执行静态检查**

运行：cd backend && pytest tests/unit/api/test_health.py -q && ruff check src tests && mypy src

预期：1 项测试通过；Ruff 和 mypy 均以 0 退出。

运行：cd web && npm test -- --run && npm run build

预期：App 测试通过，Vite 构建以 0 退出。

- [ ] **步骤 5：提交项目脚手架**

~~~bash
git add .gitignore backend web infra
git commit -m "chore: scaffold tax risk platform"
~~~

### 任务 2：实现 Money、Rate 及舍入不变量

**文件：**
- 新建：**backend/src/tax_risk/domain/money.py**
- 新建：**backend/tests/unit/domain/test_money.py**
- 新建：**backend/tests/unit/domain/test_rate_properties.py**

- [ ] **步骤 1：先编写示例测试及属性测试**

覆盖以下内容：仅允许字符串构造 Decimal、同币种运算、拒绝币种/精度不匹配、最终使用 ROUND_HALF_UP、中间过程不舍入、Rate 范围为 0..1、25%=0.25，以及 Hypothesis 生成值。决定性示例如下：

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

- [ ] **步骤 2：证明测试失败**

运行：cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py -q

预期：测试收集失败，因为 tax_risk.domain.money 缺失。

- [ ] **步骤 3：实现精确值对象契约**

Money 必须保留未舍入的 Decimal 金额、类似 ISO 的币种及非负精度；quantized 使用 Decimal.quantize 和 ROUND_HALF_UP 返回新的 Money。加减运算要求币种和精度相同。与 Rate 相乘时返回未舍入的 Money。Rate 仅接受 Decimal/字符串，统一转换为 Decimal，并拒绝 0..1 以外的值。任何位置都不得接受 float。

- [ ] **步骤 4：验证示例及属性**

运行：cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py -q

预期：全部示例及至少 100 个 Hypothesis 示例通过。

- [ ] **步骤 5：提交数值契约**

~~~bash
git add backend/src/tax_risk/domain/money.py backend/tests/unit/domain
git commit -m "feat: add exact money and rate values"
~~~

### 任务 3：创建控制面数据库及首次迁移

**文件：**
- 新建：**backend/src/tax_risk/persistence/__init__.py**
- 新建：**backend/src/tax_risk/persistence/models.py**
- 新建：**backend/src/tax_risk/persistence/repositories.py**
- 新建：**backend/src/tax_risk/persistence/ingest_models.py**
- 新建：**backend/src/tax_risk/persistence/master_models.py**
- 新建：**backend/src/tax_risk/persistence/snapshot_models.py**
- 新建：**backend/src/tax_risk/persistence/risk_models.py**
- 新建：**backend/src/tax_risk/persistence/ingest_repositories.py**
- 新建：**backend/src/tax_risk/persistence/master_repositories.py**
- 新建：**backend/src/tax_risk/persistence/snapshot_repositories.py**
- 新建：**backend/src/tax_risk/persistence/risk_repositories.py**
- 新建：**backend/alembic.ini**
- 新建：**backend/migrations/env.py**
- 新建：**backend/migrations/script.py.mako**
- 新建：**backend/migrations/versions/0001_control_plane.py**
- 新建：**backend/tests/integration/persistence/test_schema.py**
- 新建：**backend/tests/integration/persistence/test_constraints.py**

- [ ] **步骤 1：编写模式及约束测试**

测试必须断言存在 company、ingest_batch、ingest_error、tax_master_version、accounting_snapshot、snapshot_source、monitoring_run、detection_record、risk_case、review_action 及 audit_event 表。断言：

- SAP company_code 唯一，且有效/停用生命周期可审计；
- 采集来源+source_batch_key 唯一；
- source_record 的 batch_id+source_record_key 唯一；
- 税务主数据的 company+valid_from+version 唯一；
- 每个公司+期间+来源版本集合对应唯一不可变快照，并由 SnapshotSet 汇集一次 100 多家公司运行所预期的完整公司快照成员；
- SnapshotSet.published_at 仅在 PUBLISHED 状态下为非空 UTC TIMESTAMPTZ，并且仅写入一次；
- 已发布的 AccountingSnapshot、SnapshotSource、SnapshotSet 及 SnapshotSetMember 记录拒绝 UPDATE/DELETE；
- risk_case 指纹唯一；
- 计算输入/结果使用 Numeric(38,12)，并显式记录 currency/amount_scale；
- 具备 JSONB 类型的 lineage 及 formula_substitution 字段。

- [ ] **步骤 2：针对 PostgreSQL 运行并确认失败**

运行：docker compose -f infra/docker-compose.yml up -d postgres && cd backend && alembic upgrade head && pytest tests/integration/persistence -q

预期：Alembic 或测试失败，因为迁移 0001 及模型缺失。

- [ ] **步骤 3：实现模型、仓储及迁移**

使用 UUID 主键、UTC timestamptz 审计字段、用于状态的 PostgreSQL 枚举、外键及检查约束。规范 Base 保留在 `persistence/models.py` 中，规范 UnitOfWork/会话边界保留在 `persistence/repositories.py` 中；聚焦模型及仓储文件不得另行创建 Base、引擎、会话工厂或 UnitOfWork。除已列出的表外，还应包括 SourceRecord、SnapshotSet、RuleVersion 及 SnapshotSetMember。来源金额以 Numeric(38,12) 存储，Rate 值以 Numeric(20,12) 存储。在迁移 0001 中使用 PostgreSQL 触发器保护已发布的 AccountingSnapshot/SnapshotSource 及 PUBLISHED 状态的 SnapshotSet/SnapshotSetMember 记录，在 UPDATE 或 DELETE 时抛出 immutable_snapshot。

- [ ] **步骤 4：重建并验证数据库**

运行：docker compose -f infra/docker-compose.yml exec -T postgres dropdb -U tax_risk --if-exists tax_risk && docker compose -f infra/docker-compose.yml exec -T postgres createdb -U tax_risk tax_risk && cd backend && alembic upgrade head && pytest tests/integration/persistence -q

预期：迁移达到 head；所有模式/约束测试通过，包括拒绝对已发布快照执行 UPDATE。

- [ ] **步骤 5：提交持久化控制面**

~~~bash
git add backend/src/tax_risk/persistence backend/alembic.ini backend/migrations backend/tests/integration/persistence
git commit -m "feat: add auditable control-plane schema"
~~~

### 任务 4：添加 IngestBatch API 及参考批量文件适配器

**文件：**
- 新建：**backend/src/tax_risk/adapters/ingest/base.py**
- 新建：**backend/src/tax_risk/adapters/ingest/csv_adapter.py**
- 新建：**backend/src/tax_risk/application/ingest.py**
- 新建：**backend/src/tax_risk/api/schemas.py**
- 新建：**backend/src/tax_risk/api/routes/ingest.py**
- 修改：**backend/src/tax_risk/main.py**
- 新建：**backend/tests/fixtures/sap_quarterly_valid.csv**
- 新建：**backend/tests/fixtures/sap_quarterly_invalid.csv**
- 新建：**backend/tests/unit/adapters/test_csv_adapter.py**
- 新建：**backend/tests/integration/api/test_ingest_batches.py**

- [ ] **步骤 1：编写预期失败的适配器及端点测试**

定义包含 source_record_key、company_code、fiscal_year、period、currency、amount_scale、metric_code、amount 及 extracted_at 的规范记录行。测试 POST /api/v1/ingest-batches、多部分 POST /api/v1/ingest-batches/{batch_id}/files 以及 GET 状态。必需行为：

- source+source_batch_key 具有幂等性；
- 在接受财务数据集之前，dataset_code=company_master 会创建或停用 Company 记录；
- 存储 SHA-256、总行数、接受行数、拒绝行数、控制总额及 schema_version；
- 部分成功时返回带行号的错误；
- 拒绝未知公司及无效 Decimal，绝不强制转换为零；
- 重试相同文件时返回原批次，不重复创建记录。

- [ ] **步骤 2：确认测试失败**

运行：cd backend && pytest tests/unit/adapters/test_csv_adapter.py tests/integration/api/test_ingest_batches.py -q

预期：失败，因为数据采集协议、适配器及路由缺失。

- [ ] **步骤 3：实现规范适配器及用例**

定义包含 validate_header 及 iter_rows 的 BulkFileAdapter Protocol。CSV 适配器必须使用 csv.DictReader，并从字符串构造 Decimal，在读取过程中计算 SHA-256，并返回结构化行错误。应用服务负责事务以及 RECEIVED→VALIDATING→SUCCEEDED/PARTIAL/FAILED 状态流转，并存储已接受的规范 SourceRecord 记录。CompanyService 仅处理 dataset_code=company_master；未知/停用公司的财务记录必须失败。API 先接收元数据，再接收文件，并且绝不允许适配器直接写入数据库。

- [ ] **步骤 4：验证幂等性及错误报告**

运行：cd backend && pytest tests/unit/adapters/test_csv_adapter.py tests/integration/api/test_ingest_batches.py -q

预期：有效批次成功；无效文件状态为 PARTIAL，并准确列出被拒记录；重放后仅保留一个批次。

- [ ] **步骤 5：提交数据采集功能**

~~~bash
git add backend/src/tax_risk/adapters backend/src/tax_risk/application/ingest.py backend/src/tax_risk/api backend/tests/fixtures backend/tests/unit/adapters backend/tests/integration/api/test_ingest_batches.py
git commit -m "feat: ingest controlled quarterly batches"
~~~

### 任务 5：导入并解析版本化税务主数据

**文件：**
- 新建：**backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py**
- 新建：**backend/src/tax_risk/application/master_data.py**
- 新建：**backend/src/tax_risk/api/routes/master_data.py**
- 修改：**backend/src/tax_risk/api/schemas.py**
- 修改：**backend/src/tax_risk/main.py**
- 新建：**backend/tests/fixtures/tax_master_valid.xlsx**
- 新建：**backend/tests/fixtures/tax_master_duplicate.xlsx**
- 新建：**backend/tests/unit/adapters/test_tax_master_xlsx.py**
- 新建：**backend/tests/integration/application/test_master_data.py**
- 新建：**backend/tests/integration/api/test_tax_master_api.py**

- [ ] **步骤 1：编写预期失败的导入及时点查询测试**

必需列为 company_code、company_name、valid_from、valid_to、tax_rate、loss_carryforward 及 three_year_average_tax_burden。测试将 25% 和 0.25 规范化为 0.25、拒绝 0..1 以外的值、亏损额非负、拒绝公司/生效期间重复、拒绝已批准版本期间重叠、文件哈希/审计元数据、制单复核分离，以及按 company+period 查询。

- [ ] **步骤 2：确认红灯状态**

运行：cd backend && pytest tests/unit/adapters/test_tax_master_xlsx.py tests/integration/application/test_master_data.py tests/integration/api/test_tax_master_api.py -q

预期：失败，因为税务主数据导入及查询功能尚不存在。

- [ ] **步骤 3：实现分阶段导入及审批**

使用 openpyxl 的只读/仅数据模式解析 XLSX。通过 Rate 规范化百分比格式单元格及小数字符串；保留源文件名及 SHA-256。以 DRAFT 状态导入，并提供 POST /api/v1/tax-master/import、POST /api/v1/tax-master/{version_id}/approve 及 GET /api/v1/tax-master/{company_code}?period=YYYY-QN。审批必须拒绝由上传人担任复核人，并在同一事务内拒绝期间重叠。

- [ ] **步骤 4：验证主数据安全性**

运行：cd backend && pytest tests/unit/adapters/test_tax_master_xlsx.py tests/integration/application/test_master_data.py tests/integration/api/test_tax_master_api.py -q

预期：有效导入及独立审批通过；重复、期间重叠、公司缺失及同一人审批均以稳定错误码失败。

- [ ] **步骤 5：提交税务主数据功能**

~~~bash
git add backend/src/tax_risk/adapters/ingest/tax_master_xlsx.py backend/src/tax_risk/application/master_data.py backend/src/tax_risk/api backend/tests/fixtures/tax_master_*.xlsx backend/tests/unit/adapters/test_tax_master_xlsx.py backend/tests/integration
git commit -m "feat: govern versioned tax master data"
~~~

### 任务 6：通过质量门禁发布不可变快照

**文件：**
- 新建：**backend/src/tax_risk/application/snapshots.py**
- 新建：**backend/src/tax_risk/api/routes/snapshots.py**
- 修改：**backend/src/tax_risk/api/schemas.py**
- 修改：**backend/src/tax_risk/main.py**
- 新建：**backend/tests/unit/application/test_snapshot_quality.py**
- 新建：**backend/tests/integration/application/test_snapshot_publication.py**
- 新建：**backend/tests/integration/api/test_snapshots_api.py**

- [ ] **步骤 1：编写预期失败的质量门禁测试**

定义必需的季度指标代码：cumulative_profit、received_dividends、fair_value_change、cumulative_revenue、prior_quarter_current_tax、current_quarter_current_tax、other_payables_accrual、hesi_no_invoice。测试：

- 所有必需的来源批次均已成功，或部分成功已被明确接受；
- company/period/currency/amount_scale 一致；
- 能解析到一个已批准的税务主数据版本；
- 指标重复及控制总额不匹配会阻止发布；
- 主数据或来源缺失时创建 DATA_QUALITY 监测结果，而不是填零；
- 对于相同且有序的来源哈希，快照哈希保持稳定；
- 一个 SnapshotSet 可为每个请求的公司及期间恰好包含一个已发布公司快照；
- 任一预期成员缺失、无效、未锁定、期间混杂、重复或未通过质量门禁时，SnapshotSet 发布失败，且不写入集合、成员或 published_at；
- 所有预期成员均在同一事务中锁定、插入，并与 SnapshotSet 一起原子流转至 PUBLISHED；
- published_at 由数据库以 UTC 生成且仅生成一次，API 将其作为带时区的 RFC 3339 UTC 值返回，并作为阶段 4 唯一的 data_ready_at；
- 已发布的 AccountingSnapshot/SnapshotSource 及 PUBLISHED 状态的 SnapshotSet/SnapshotSetMember 记录拒绝 UPDATE/DELETE。

- [ ] **步骤 2：证明质量检查尚不存在**

运行：cd backend && pytest tests/unit/application/test_snapshot_quality.py tests/integration/application/test_snapshot_publication.py tests/integration/api/test_snapshots_api.py -q

预期：失败，因为快照质量及发布服务缺失。

- [ ] **步骤 3：实现先校验后发布**

创建 POST /api/v1/snapshots/validate、POST /api/v1/snapshots/{id}/publish 及 POST /api/v1/snapshot-sets。校验返回 error_code、source、field、company、period 及 remediation。发布 AccountingSnapshot 时锁定来源批次 ID 及已批准税务主数据 ID，基于规范元数据计算确定性 SHA-256，并在同一事务中完成 DRAFT→VALIDATED→PUBLISHED 流转。

发布 SnapshotSet 时接收完整的预期成员清单，锁定所有引用的已发布 AccountingSnapshot 及来源/主数据成员关系，重新运行集合级质量门禁，插入全部 SnapshotSetMember 记录，将集合流转至 PUBLISHED，并在同一事务中仅写入一次数据库 UTC `published_at`。任一成员失败或并发变更都会回退整个事务，不留下 SnapshotSet、成员或时间戳。响应模式以带时区的 RFC 3339 UTC 值返回 `published_at`。PUBLISHED 集合及其成员不可变；如需修正成员关系，应创建带 `supersedes_snapshot_set_id` 的新集合。后续阶段必须以 SnapshotSet.published_at 作为 `data_ready_at`，不得使用上传、模型调用或批次开始时间戳。

- [ ] **步骤 4：运行快照测试及工作块 1 回归测试**

运行：cd backend && pytest tests/unit tests/integration -q && ruff check src tests && mypy src

预期：工作块 1 的全部测试通过；集合发布失败时不留下记录/时间戳；发布成功时写入一个 UTC published_at 及不可变的完整成员关系；此时尚不存在规则或 Agent 测试。

- [ ] **步骤 5：提交并标记工作块 1 检查点**

~~~bash
git add backend/src/tax_risk/application/snapshots.py backend/src/tax_risk/api backend/tests
git commit -m "feat: publish quality-gated immutable snapshots"
~~~

开始工作块 2 前，对照源规格说明书执行计划/文档检查点，确认工作块 1 不包含语义 Agent、向量、提示词或模型依赖。

## 工作块 2：季度规则、风险事项、并行执行及产品切片

### 任务 7：实现三项确定性季度计算

**文件：**
- 新建：**backend/src/tax_risk/domain/quarterly.py**
- 新建：**backend/tests/unit/domain/test_quarterly_examples.py**
- 新建：**backend/tests/unit/domain/test_quarterly_properties.py**
- 新建：**backend/tests/unit/domain/test_quarterly_errors.py**

- [ ] **步骤 1：先编写已批准示例及边界场景**

测试必须断言以下标准示例：

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

还需测试利润为零/负数、公允价值变动为负、亏损全额/部分抵减、红字记录、本季度应计提金额为负、营业收入≤0 时税负率取0并继续判断偏离，以及仅在最终结果舍入。`received_dividends` 是 SAP 账簿中本年累计**收到**的分红金额，不包括公司支付/分配的股利，并保留 SAP 冲销符号。`historical_average_tax_burden` 从已批准的公司主数据版本匹配，平台绝不重新计算。

锁定以下取零下限前边界：

- `base_before_floor=-100` 且 `potential_adjustment=60` 时，结果为 `cumulative_base=0`、`potential_base=0`、潜在税务成本为零，且不触发潜在税务成本预警。
- `base_before_floor=-100` 且 `potential_adjustment=150` 时，结果为 `cumulative_base=0` 及 `potential_base=50`；税率为 25% 时，潜在应纳税额/成本为 12.50，并触发 `POTENTIAL_TAX_COST`。

锁定全部预警边界：

- 本季度差异 >0 → `UNDER_ACCRUED`；<0 → `OVER_ACCRUED`；=0 → 不触发计提预警；
- 税负率偏离度 >=+0.05 → `TAX_BURDEN_HIGH`；<=-0.05 → `TAX_BURDEN_LOW`；-0.05<偏离度<+0.05 → 不触发税负率预警；
- 潜在税务成本 !=0 → `POTENTIAL_TAX_COST`；=0 → 不触发潜在税务成本预警。

添加舍入敏感场景，以证明税负率的分子是已按 ROUND_HALF_UP 舍入的本年累计应纳税额，而非未舍入的税额乘积。Hypothesis 属性：本年累计应纳税额非负；增加非负的潜在调增金额不会使潜在应纳税额减少；相同输入的结果具有确定性。

- [ ] **步骤 2：运行并观察失败**

运行：cd backend && pytest tests/unit/domain/test_quarterly_examples.py tests/unit/domain/test_quarterly_properties.py tests/unit/domain/test_quarterly_errors.py -q

预期：失败，因为季度领域类型及 calculate_quarterly 缺失。

- [ ] **步骤 3：唯一实现一套纯公式**

实现不可变的 QuarterlyInputs 及 QuarterlyResult。使用以下公式，Decimal 中间结果不舍入：

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

不得从已取零下限的 `cumulative_base` 推导 `potential_base`。`cumulative_tax` 和 `potential_tax` 分别按公司账簿精度使用 ROUND_HALF_UP 舍入一次；税负率以舍入后的 `cumulative_tax` 为分子，Decimal 除法结果不舍入。将该不依赖展示格式的偏离度与 Decimal("0.05") 比较。不得对 `current_should_accrue` 应用 max。

输出 currency、amount_scale、CALCULATED/NOT_CALCULABLE/FAILED、预警标志/代码、可空值、not_calculated_reason，以及同时包含 `base_before_floor` 和 `cumulative_base` 的 formula_substitution 映射。计算器接收已批准主数据提供的前三个完整年度平均税负率；不存在计算历史平均值的代码路径。

- [ ] **步骤 4：验证全部数值契约**

运行：cd backend && pytest tests/unit/domain/test_quarterly_*.py -q

预期：标准值及 `POTENTIAL_TAX_COST` 完全匹配；-100+60 和 -100+150 的取零下限前场景通过；计提差异为正/负/零、税负率偏离度精确等于 ±5 个百分点/位于区间内部，以及潜在税务成本非零/为零的预警边界均通过；营业收入≤0 时税负率为0并继续计算偏离度；全部属性测试通过。

- [ ] **步骤 5：提交公式实现**

~~~bash
git add backend/src/tax_risk/domain/quarterly.py backend/tests/unit/domain/test_quarterly_*.py
git commit -m "feat: calculate quarterly tax risks deterministically"
~~~

### 任务 8：持久化监测结果并强制执行风险指纹/状态机

**文件：**
- 新建：**backend/src/tax_risk/domain/cases.py**
- 新建：**backend/src/tax_risk/application/quarterly_runs.py**
- 新建：**backend/tests/unit/domain/test_case_fingerprint.py**
- 新建：**backend/tests/unit/domain/test_case_state_machine.py**
- 新建：**backend/tests/integration/application/test_quarterly_run.py**

- [ ] **步骤 1：编写预期失败的风险事项测试**

测试数值指纹是 company_code|fiscal_year|quarter|monitoring_type 的 SHA-256，不包含规则/模型版本。不同季度/监测类型不得发生碰撞。定义允许的状态流转：

NEW→ASSIGNED→PENDING_COMPANY_CONFIRMATION;
确认需调整的分支：PENDING_ADJUSTMENT→ADJUSTED_PENDING_REVIEW→CLOSED；
判定合理的分支：GROUP_REVIEW→CLOSED；
需补充资料的分支：EVIDENCE_REQUIRED→PENDING_COMPANY_CONFIRMATION。

测试非法流转失败，并保留每条监测结果。风险事项按监测类型隔离创建：

- ACCRUAL_ACCURACY 仅在 `UNDER_ACCRUED` 或 `OVER_ACCRUED` 时创建风险事项，差异为零时绝不创建；
- TAX_BURDEN 仅在 `TAX_BURDEN_HIGH` 或 `TAX_BURDEN_LOW` 时创建风险事项，包括精确等于 ±0.05 的情况；偏离度位于区间内部或不可计算时绝不创建；
- POTENTIAL_TAX_COST 仅在成本非零且代码为 `POTENTIAL_TAX_COST` 时创建风险事项。

对于同一公司/季度，某项监测的预警或零值不得创建、抑制、关闭或覆盖另一项监测的风险事项。三项预警同时存在的夹具创建三个不同的指纹/风险事项；混合夹具只创建实际触发预警的子集。重跑时增加监测结果但不重复创建风险事项，并且 DATA_QUALITY/NOT_CALCULABLE 绝不能显示为“无风险”。

- [ ] **步骤 2：确认红灯状态**

运行：cd backend && pytest tests/unit/domain/test_case_*.py tests/integration/application/test_quarterly_run.py -q

预期：失败，因为指纹、状态流转及季度运行服务缺失。

- [ ] **步骤 3：实现风险事项及运行事务**

QuarterlyRunService 加载一个 PUBLISHED 快照及一个代码为 QUARTERLY_V1 的已批准 RuleVersion，构造 QuarterlyInputs，执行纯计算器，为每种监测类型写入一条 DetectionRecord，并按指纹新增或更新 RiskCase。持久化公式代入过程、快照/规则/主数据版本、来源血缘、币种、精度、calculation_status、预警代码及方向。通过迁移/数据准备代码存储已复核的 QUARTERLY_V1 公式清单及 SHA-256，而不是通过 API 接受自由格式公式。使用事务及 PostgreSQL 唯一约束确保重试安全。

- [ ] **步骤 4：验证持久化及重试行为**

运行：cd backend && pytest tests/unit/domain/test_case_*.py tests/integration/application/test_quarterly_run.py -q

预期：合法流转通过；非法流转失败；预警方向/零值/±0.05 边界恰好创建预期的隔离风险事项子集；运行两次时产生两组监测结果，但每个触发预警的监测类型仅有一个风险事项。

- [ ] **步骤 5：提交风险事项生命周期**

~~~bash
git add backend/src/tax_risk/domain/cases.py backend/src/tax_risk/application/quarterly_runs.py backend/tests/unit/domain/test_case_*.py backend/tests/integration/application/test_quarterly_run.py
git commit -m "feat: create idempotent quarterly risk cases"
~~~

### 任务 9：使用 Celery 编排 100 多家公司的季度批次

**文件：**
- 新建：**backend/src/tax_risk/workers/celery_app.py**
- 新建：**backend/src/tax_risk/workers/quarterly_batch.py**
- 新建：**backend/tests/unit/workers/test_quarterly_batch_canvas.py**
- 新建：**backend/tests/integration/workers/test_quarterly_batch_eager.py**
- 新建：**backend/tests/integration/workers/test_quarterly_batch_105_companies.py**
- 修改：**infra/docker-compose.yml**

- [ ] **步骤 1：编写扇出、幂等性及隔离测试**

创建 105 家公司：103 家有效、1 家缺失主数据、1 家来源批次格式错误。断言编排器为每个 SnapshotSet 成员构建一个公司任务，按 run_type=quarterly 路由，限制可配置并发数，使用 fiscal_year+quarter+snapshot_set_id+rule_version 作为运行键，按公司记录成功/已阻断/失败状态，允许仅重试失败公司，并且最终汇总为 103 家成功、2 家已阻断/失败，不回退成功结果。

- [ ] **步骤 2：确认 Celery 测试失败**

运行：cd backend && pytest tests/unit/workers/test_quarterly_batch_canvas.py tests/integration/workers/test_quarterly_batch_eager.py tests/integration/workers/test_quarterly_batch_105_companies.py -q

预期：失败，因为 Celery 应用及任务缺失。

- [ ] **步骤 3：实现 group/chord 编排**

配置仅使用 JSON 序列化、UTC 时间戳、任务完成后确认、reject-on-worker-lost、任务时限、retry_backoff、retry_jitter 及独立季度队列。使用由 run_company_quarterly 任务组成的 Celery group，随后执行 summarize_quarterly_batch。公司任务调用 QuarterlyRunService，仅返回 ID/状态，绝不返回完整财务记录。除 Celery 任务 ID 外，还需强制执行数据库幂等性。

- [ ] **步骤 4：验证 105 家公司场景行为**

运行：cd backend && CELERY_TASK_ALWAYS_EAGER=true pytest tests/unit/workers tests/integration/workers -q

预期：全部测试通过；103 家成功公司的结果保持提交；重试两家失败公司不会创建重复风险事项。

- [ ] **步骤 5：提交编排功能**

~~~bash
git add backend/src/tax_risk/workers backend/tests/unit/workers backend/tests/integration/workers infra/docker-compose.yml
git commit -m "feat: run quarterly monitoring across companies"
~~~

### 任务 10：提供最小化安全季度 API

**文件：**
- 新建：**backend/src/tax_risk/security/principal.py**
- 新建：**backend/src/tax_risk/api/dependencies.py**
- 新建：**backend/src/tax_risk/api/routes/runs.py**
- 新建：**backend/src/tax_risk/api/routes/cases.py**
- 新建：**backend/src/tax_risk/api/routes/dashboard.py**
- 修改：**backend/src/tax_risk/api/schemas.py**
- 修改：**backend/src/tax_risk/main.py**
- 新建：**backend/tests/integration/api/test_quarterly_runs_api.py**
- 新建：**backend/tests/integration/api/test_cases_scope.py**
- 新建：**backend/tests/integration/api/test_dashboard_api.py**

- [ ] **步骤 1：编写预期失败的 API 及公司范围测试**

必需端点：

- POST /api/v1/quarterly-runs，参数包括 fiscal_year、quarter、snapshot_set_id、rule_version；
- GET /api/v1/quarterly-runs/{run_id};
- GET /api/v1/risk-cases，参数包括 year、quarter、monitoring_type、direction、status、company；
- POST /api/v1/risk-cases/{case_id}/actions;
- GET /api/v1/dashboard/quarterly?fiscal_year=&quarter=;
- GET /api/v1/detections/{detection_id}，用于查询公式代入过程及数据血缘。

测试集团税务主体可查看全部数据；公司财务主体只能查看本公司数据；审计主体为只读；无权访问的公司 ID 返回 404 而不是泄露信息的 403；响应中的 Decimal 值为字符串，并包含 currency/scale/status/reason 字段。

- [ ] **步骤 2：确认端点测试失败**

运行：cd backend && pytest tests/integration/api/test_quarterly_runs_api.py tests/integration/api/test_cases_scope.py tests/integration/api/test_dashboard_api.py -q

预期：因路由返回 404 或主体依赖缺失而失败。

- [ ] **步骤 3：实现 API 及服务端范围控制**

创建包含 subject、roles、allowed_company_ids 及 organization_path 的 Principal。仅在开发环境中，允许在 development_principal_enabled 控制下使用签名测试请求头；生产环境必须使用注入的 IdP 验证器。在仓储 SQL 中应用范围控制，并保持 PostgreSQL RLS 可迁移。驾驶舱返回 coverage_company_count、data_ready_count、blocked_count、risk_company_count、potential_tax_cost_total、各监测类型数量及分页公司记录。

- [ ] **步骤 4：验证 API 行为及 OpenAPI**

运行：cd backend && pytest tests/integration/api/test_* -q && python -c "from tax_risk.main import create_app; assert create_app().openapi()['paths']['/api/v1/dashboard/quarterly']"

预期：全部 API 测试通过；响应中不存在未授权数据；OpenAPI 包含季度驾驶舱路径。

- [ ] **步骤 5：提交 API 切片**

~~~bash
git add backend/src/tax_risk/security backend/src/tax_risk/api backend/tests/integration/api
git commit -m "feat: expose scoped quarterly risk APIs"
~~~

### 任务 11：构建最小可用季度驾驶舱

**文件：**
- 新建：**web/src/api/client.ts**
- 新建：**web/src/api/quarterly.ts**
- 新建：**web/src/features/quarterly/types.ts**
- 新建：**web/src/features/quarterly/QuarterlyDashboardPage.tsx**
- 新建：**web/src/features/quarterly/QuarterlyRunTable.tsx**
- 新建：**web/src/features/quarterly/FormulaDrawer.tsx**
- 新建：**web/src/features/quarterly/QuarterlyDashboardPage.test.tsx**
- 新建：**web/src/features/quarterly/FormulaDrawer.test.tsx**
- 修改：**web/src/App.tsx**

- [ ] **步骤 1：编写预期失败的用户界面组件测试**

模拟 TanStack Query 响应并断言：

- 卡片展示覆盖公司数、数据就绪公司数、阻断公司数、异常公司数及潜在税务成本；
- 选择器会更改年度/季度查询键；
- 表格将数据质量阻断记录与风险记录分开；
- 渲染风险类型、方向、实际值/应计值/差异值及状态；
- 抽屉展示公式、每个代入值、来源、快照、主数据版本及规则版本；
- 营业收入≤0 时展示税负率0及其与历史平均税负率的偏离结果；
- 不出现 Agent 或语义风险导航。

- [ ] **步骤 2：确认界面处于红灯状态**

运行：cd web && npm test -- --run src/features/quarterly

预期：失败，因为季度组件及 API 函数缺失。

- [ ] **步骤 3：实现类型化驾驶舱**

使用 Ant Design 的 Statistic、Alert、Select、Table、Tag、Drawer 及 Descriptions。TanStack Query 管理服务端状态；筛选条件保存在 URL 查询参数中；根据字符串+币种+精度渲染金额，不使用 JavaScript 浮点运算。详情抽屉调用监测结果端点，不在浏览器中重新计算公式。

- [ ] **步骤 4：验证组件及生产构建**

运行：cd web && npm test -- --run && npm run lint && npm run build

预期：全部组件测试通过；lint 以 0 退出；Vite 生成生产构建包。

- [ ] **步骤 5：提交驾驶舱**

~~~bash
git add web/src
git commit -m "feat: add quarterly tax risk dashboard"
~~~

### 任务 12：完成全栈端到端验收及操作说明

**文件：**
- 新建：**backend/tests/e2e/test_quarterly_standard_scenario.py**
- 新建：**backend/tests/e2e/test_quarterly_api_worker_flow.py**
- 新建：**backend/tests/e2e/seed_quarterly_scenario.py**
- 新建：**web/e2e/quarterly-dashboard.spec.ts**
- 新建：**web/playwright.config.ts**
- 修改：**infra/docker-compose.yml**
- 新建：**infra/README.md**
- 新建：**backend/README.md**
- 新建：**web/README.md**

- [ ] **步骤 1：编写预期失败的端到端验收测试**

准备 105 家公司的数据，其中包括已批准的标准公司：

- 利润总额 10,000,000；收到分红 1,000,000；公允价值变动收益 500,000；可弥补以前年度亏损 2,000,000；税率 0.25；
- 以前季度所得税计提额 900,000；本季度所得税计提额 700,000；营业收入 50,000,000；历史平均税负率 0.09；
- 其他应付款暂估余额 1,400,000；合思无票报销金额 300,000。

API 端到端测试必须断言：本年累计应纳税额为 1,625,000.00，应计提额为 725,000.00，差异为 +25,000.00，本年累计税负率为 0.0325，偏离度为 -0.0575，潜在调增金额为 1,700,000.00，潜在应纳税额为 2,050,000.00，潜在税务成本为 425,000.00，潜在预警代码为 `POTENTIAL_TAX_COST`。浏览器端到端测试必须定位该公司、打开公式详情，并展示相同的来源值及版本。

- [ ] **步骤 2：运行端到端测试并确认接线前失败**

运行：docker compose -f infra/docker-compose.yml up -d --build && cd backend && pytest tests/e2e/test_quarterly_standard_scenario.py -q

预期：在数据准备、完整 API 路由接线及工作任务流程完成前失败。

运行：cd web && npx playwright test e2e/quarterly-dashboard.spec.ts

预期：在运行中的驾驶舱能够加载已准备结果前失败。

- [ ] **步骤 3：配置具备生产形态的 Compose 拓扑**

在 `infra/docker-compose.yml` 中配置 postgres、redis、一次性 migrate、api、worker-quarterly 及 web 服务。为每个长时间运行的服务配置健康检查；使用依赖健康条件而不是休眠等待；生产形态容器不挂载源代码目录；不暴露语义 Agent/模型服务。

- [ ] **步骤 4：验证 Compose 拓扑**

运行：

~~~bash
docker compose -f infra/docker-compose.yml config --quiet
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml ps
~~~

预期：config 以 0 退出；postgres、redis、api、worker-quarterly 及 web 正在运行且健康；migrate 成功退出；不存在模型服务。

- [ ] **步骤 5：接通 API-工作任务-Web 验收路径**

使用 `seed_quarterly_scenario.py`，通过阶段 1 公共数据采集/主数据/快照接口加载 105 家公司夹具，原子发布其 SnapshotSet，通过 API 提交季度运行，让 `worker-quarterly` 处理公司任务，轮询运行端点直至获得终态汇总，并由 Web 驾驶舱读取持久化结果。`test_quarterly_api_worker_flow.py` 必须使用服务 URL 及 API 契约，不得直接导入应用服务。

- [ ] **步骤 6：验证聚焦的 API-工作任务-Web 路径**

运行：

~~~bash
cd backend
pytest tests/e2e/test_quarterly_api_worker_flow.py tests/e2e/test_quarterly_standard_scenario.py -q
cd ../web
npx playwright test e2e/quarterly-dashboard.spec.ts
~~~

预期：两个后端端到端测试及 Playwright 均通过；103 家有效公司的结果保持提交，两家被阻断公司保持隔离，浏览器公式抽屉展示已持久化的 API 值。

- [ ] **步骤 7：编写基础设施操作手册**

编写 `infra/README.md`，提供可准确复制执行的命令，涵盖前置条件、环境配置、启动、健康检查、数据库迁移、数据准备、季度提交、状态轮询、失败公司重试、日志导出、测试备份/恢复及停止。明确 SnapshotSet.published_at 是唯一的数据就绪时间戳，且阶段 1 不需要模型凭据。

- [ ] **步骤 8：验证基础设施手册契约**

运行：`rg -n '^## (前置条件|配置|启动|健康检查|数据库迁移|数据准备|提交|检查|重试|日志|备份与恢复|停止)$' infra/README.md`

预期：十二个必需章节各出现一次，且其中的命令引用 `infra/docker-compose.yml`。

- [ ] **步骤 9：编写后端手册**

编写 `backend/README.md`，涵盖 Python 3.12 环境准备、数据库配置、Alembic 升级/在一次性副本上降级的命令、API 端点、Celery 季度队列、仅重试指定公司、单元/集成/端到端测试命令、Decimal/ROUND_HALF_UP 规则，以及禁止语义 Agent 依赖的要求。

- [ ] **步骤 10：验证后端手册契约**

运行：`rg -n '^## (环境准备|数据库|数据库迁移|API|季度任务|重试|测试|数值契约|第一阶段边界)$' backend/README.md`

预期：全部必需的后端章节各出现一次。

- [ ] **步骤 11：编写 Web 手册**

编写 `web/README.md`，涵盖 Node 安装、`VITE_API_BASE_URL`、本地开发启动、Vitest、代码检查/类型检查、生产构建、Playwright 端到端测试及季度驾驶舱路由。

- [ ] **步骤 12：验证 Web 手册契约**

运行：`rg -n '^## (环境准备|环境变量|本地开发|单元测试|代码检查与类型检查|构建|浏览器端到端测试|季度监测看板)$' web/README.md`

预期：全部必需的 Web 章节各出现一次，且文档中没有模型/Agent 配置。

- [ ] **步骤 13：运行完整验证并记录预期证据**

运行：cd backend && pytest --cov=tax_risk --cov-report=term-missing -q && ruff check src tests && mypy src

预期：全部后端测试通过；domain/quarterly.py 及 domain/money.py 的分支覆盖率为 100%；后端总体覆盖率至少为 90%；Ruff 和 mypy 均以 0 退出。

运行：cd web && npm test -- --run && npm run lint && npm run build && npx playwright test

预期：全部单元/组件/浏览器测试通过；lint 及构建均以 0 退出。

运行：docker compose -f infra/docker-compose.yml ps

预期：postgres、redis、api、worker-quarterly 及 web 均健康；migrate 已完成；不存在语义 Agent/模型服务。

- [ ] **步骤 14：提交阶段 1 验收切片**

~~~bash
git add backend/tests/e2e backend/README.md web/e2e web/playwright.config.ts web/README.md infra
git commit -m "test: verify phase one quarterly monitoring"
~~~

工作块 2 结束时，将实现与源规格说明书第 2、5、6.1–6.3、7、9、11、12.1、12.3 及 13.1 节进行对比。将任何已接受的偏差记录在相关 README 中，并在生产部署前，就字段映射、金额精度及 105 家公司验收报告取得业务签字确认。

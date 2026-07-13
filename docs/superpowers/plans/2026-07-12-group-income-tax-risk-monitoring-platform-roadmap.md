# 集团所得税风险监测平台实施计划

> **面向 Agent 工作单元：** 必须使用 superpowers:subagent-driven-development（如可使用子 Agent）或 superpowers:executing-plans 来实施本计划。各步骤使用复选框（`- [ ]`）语法跟踪。

**目标：** 通过四个可独立验证的阶段，为 100 多家公司交付已确认的 V0.8 平台，同时确保公式准确、证据可追溯、语义 Agent 边界清晰且上线过程可回退。

**架构：** 构建一个模块化单体应用，涵盖批量采集、版本化主数据、不可变快照、确定性季度规则、供应商中立的语义 Agent、统一风险事项以及人工复核。按顺序执行关联的阶段计划。轻量级验收工具记录每个阶段的命令、输出哈希、Git 修订版本、阈值结果和制品；证据缺失或无效时阻止晋级。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy/Alembic、PostgreSQL、Celery/Redis、pytest/Hypothesis、React/TypeScript/Vite/Ant Design/TanStack Query、Vitest/Playwright、OpenTelemetry、Docker Compose、CI 工作流。

---

## 工作块 1：四阶段交付与验收编排

### 任务 1：构建验收工具并锁定跨阶段契约

**文件：**
- 新建：`scripts/acceptance/record_command.py`
- 新建：`scripts/acceptance/verify_artifact.py`
- 新建：`scripts/acceptance/verify_contract_registry.py`
- 新建：`scripts/acceptance/tests/test_acceptance_tools.py`
- 新建：`scripts/acceptance/tests/fixtures/valid_command_result.json`
- 新建：`scripts/acceptance/tests/fixtures/invalid_command_result.json`
- 新建：`artifacts/acceptance/.gitkeep`

权威输入：

- `docs/superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md`
- `docs/design/architecture/2026-07-12-group-income-tax-risk-monitoring-platform-architecture.md`
- `docs/design/function/2026-07-12-group-income-tax-risk-monitoring-platform-function.md`
- `docs/design/detailed/2026-07-12-group-income-tax-risk-monitoring-platform-detailed.md`

文档之间存在差异时，以 V0.8 规格说明书为准。公式、阈值、源系统、证据优先级或自动化边界如需变更，必须先批准规格说明书修订，再修改代码。

规范共享路径：

| 职责 | 规范路径 |
|---|---|
| 主体与组织范围 | `backend/src/tax_risk/security/principal.py` |
| FastAPI 依赖 | `backend/src/tax_risk/api/dependencies.py` |
| 风险指纹与生命周期 | `backend/src/tax_risk/domain/cases.py` |
| 应用用例 | `backend/src/tax_risk/application/` |
| ORM 模型与仓储 | `backend/src/tax_risk/persistence/models.py`, `backend/src/tax_risk/persistence/repositories.py` |
| 供应商中立的模型端口 | `backend/src/tax_risk/application/semantic/model_client.py` |
| 风险与驾驶舱路由 | `backend/src/tax_risk/api/routes/cases.py`, `backend/src/tax_risk/api/routes/dashboard.py` |
| 路由与工作任务注册 | `backend/src/tax_risk/main.py`, `backend/src/tax_risk/workers/celery_app.py` |
| 前端根组件 | `web/src/App.tsx` |

规范线性迁移登记表：

| 阶段 | 迁移 | `down_revision` |
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

- [ ] **步骤 1：编写预期失败的验收工具测试**

测试 `record_command.py` 是否在不使用 shell 插值的情况下运行 argv 数组，是否写入配套日志和 JSON（包含名称、argv、UTC 开始/结束时间、退出码、Git 修订版本、stdout/stderr SHA-256 以及引用输入的哈希），并返回子进程退出码。测试 `verify_artifact.py` 是否拒绝文件缺失、退出码非零、哈希不匹配或必填字段缺失的情况。使用临时的有效和损坏的计划/迁移夹具测试契约登记表。

- [ ] **步骤 2：运行验收工具测试并确认红灯状态**

运行：`python3 -m unittest discover -s scripts/acceptance/tests -p 'test_*.py'`

预期：失败，因为三个验收模块尚不存在。

- [ ] **步骤 3：实现三个仅依赖标准库的工具**

`record_command.py` 必须以 argv 列表调用 `subprocess.run`，绝不能使用 `shell=True`。每个 JSON/日志制品都先刷新到同目录临时文件，执行 `fsync` 后再原子重命名；发生中断时，只能保留上一份完整制品或不留下最终制品。`verify_artifact.py` 校验命令结果 JSON 及所有必需的 JUnit/JSON 配套文件。`verify_contract_registry.py` 解析五份计划文件，后续再解析 Alembic 目录；遇到重复的规范职责或非线性迁移链时失败，并将已检查路径、修订版本、输入哈希和结果写入输出。

- [ ] **步骤 4：运行测试并生成仅含计划的登记制品**

运行：

```bash
python3 -m unittest discover -s scripts/acceptance/tests -p 'test_*.py'
python3 scripts/acceptance/verify_contract_registry.py \
  --plans-dir docs/superpowers/plans \
  --mode plans-only \
  --output artifacts/acceptance/contract-registry.json
python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/contract-registry.json
```

预期：所有命令均以 0 退出；登记表记录 `0001 -> 0002a -> 0002b -> 0002c -> 0002d -> 0003 -> 0004 -> 0005 -> 0006 -> 0007` 以及全部规范路径。

- [ ] **步骤 5：提交验收工具**

```bash
git add scripts/acceptance artifacts/acceptance/.gitkeep artifacts/acceptance/contract-registry.json
git commit -m "test(acceptance): record phased delivery evidence"
```

### 任务 2：实施并验收阶段 1 确定性监测

**文件：**
- 执行：`docs/superpowers/plans/2026-07-12-phase-1-foundation-quarterly.md`
- 生成：`artifacts/acceptance/phase-1/formula-report.xml`
- 生成：`artifacts/acceptance/phase-1/formula-command.json`
- 生成：`artifacts/acceptance/phase-1/full-stack.xml`
- 生成：`artifacts/acceptance/phase-1/full-stack-command.json`

- [ ] **步骤 1：执行阶段 1 的全部任务并提交检查点**

在阶段 1 计划完成前不得继续。公式准确性、可追溯性、主数据质量阻断、单家公司失败隔离以及不存在语义模型依赖，均属于阻断性契约。

- [ ] **步骤 2：记录确定性公式门禁**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-1-formulas \
  --output artifacts/acceptance/phase-1/formula-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/domain/test_money.py tests/unit/domain/test_rate_properties.py tests/unit/domain/test_quarterly_*.py -q --junitxml=../artifacts/acceptance/phase-1/formula-report.xml'
```

预期：以 0 退出；全部已批准示例、账务舍入、精确的 5 个百分点边界、取零下限前潜在计税基础为负的场景，以及属性测试全部通过。

- [ ] **步骤 3：记录集成、105 家公司及浏览器门禁**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-1-full-stack \
  --output artifacts/acceptance/phase-1/full-stack-command.json \
  -- bash -lc 'cd backend && pytest tests/integration tests/e2e/test_quarterly_standard_scenario.py -q --junitxml=../artifacts/acceptance/phase-1/full-stack.xml && cd ../web && npm test -- --run && npx playwright test e2e/quarterly-dashboard.spec.ts'
```

预期：以 0 退出；某家公司失败时，其他成功公司的结果仍保持提交；每项结果都展示快照、主数据和规则血缘；不存在 LLM、提示词、向量或 Agent 依赖。

- [ ] **步骤 4：校验并提交阶段 1 证据**

运行：`python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-1/formula-command.json artifacts/acceptance/phase-1/formula-report.xml artifacts/acceptance/phase-1/full-stack-command.json artifacts/acceptance/phase-1/full-stack.xml`

预期：以 0 退出，不存在文件缺失或哈希不匹配。

```bash
git add artifacts/acceptance/phase-1
git commit -m "test(acceptance): approve deterministic tax foundation"
```

### 任务 3：实施并验收阶段 2 业务招待费监测

**文件：**
- 执行：`docs/superpowers/plans/2026-07-12-phase-2-business-entertainment-agent.md`
- 生成：`artifacts/acceptance/phase-2/backend.xml`
- 生成：`artifacts/acceptance/phase-2/backend-command.json`
- 生成：`artifacts/acceptance/phase-2/web-command.json`

- [ ] **步骤 1：执行阶段 2 的全部任务并提交检查点**

除非满足以下条件，否则阻止完成：五个源数据集全部使用 IngestBatch/快照；强制执行有效公司清单；已关联 SAP 与未关联业务单据的路径保持分离；孤立 SAP 数据仅计入覆盖范围；后续形成的精确关联由服务端重新校验并合并，且不会重复暴露风险。

- [ ] **步骤 2：记录后端、安全性、黄金集及端到端证据**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-2-backend \
  --output artifacts/acceptance/phase-2/backend-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/business_entertainment tests/unit/semantic tests/integration/application tests/integration/api/test_business_entertainment_api.py tests/integration/api/test_entertainment_export.py tests/integration/workers/test_business_entertainment_worker.py tests/security tests/evaluation/test_golden_governance.py tests/evaluation/test_business_entertainment_metrics.py tests/e2e/test_business_entertainment_pipeline.py -q --junitxml=../artifacts/acceptance/phase-2/backend.xml'
```

预期：以 0 退出；已知典型场景零漏检，试点召回率至少为 90%，正式门禁召回率至少为 95%，高置信度准确率至少为 80%，且全部关联、合并及 KPI 不变量均通过。

- [ ] **步骤 3：记录前端及浏览器证据**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-2-web \
  --output artifacts/acceptance/phase-2/web-command.json \
  -- bash -lc 'cd web && npm test -- --run && npm run build && npx playwright test e2e/business-entertainment.spec.ts'
```

预期：以 0 退出；界面展示 `SAP凭证待定位`、精确证据、科目建议、覆盖口径、合并历史及单一有效汇总数。

- [ ] **步骤 4：校验并提交阶段 2 证据**

运行：`python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-2/backend-command.json artifacts/acceptance/phase-2/backend.xml artifacts/acceptance/phase-2/web-command.json`

预期：以 0 退出。

```bash
git add artifacts/acceptance/phase-2
git commit -m "test(acceptance): approve entertainment evidence paths"
```

### 任务 4：实施并验收阶段 3 福利费与捐赠支出监测

**文件：**
- 执行：`docs/superpowers/plans/2026-07-12-phase-3-welfare-donation-agents.md`
- 生成：`artifacts/acceptance/phase-3/backend.xml`
- 生成：`artifacts/acceptance/phase-3/backend-command.json`
- 生成：`artifacts/acceptance/phase-3/web-command.json`

- [ ] **步骤 1：执行阶段 3 的全部任务并提交检查点**

除非以下项目全部通过，否则阻止完成：精确的 14%/12% 大于零门禁、输入缺失时的行为、完整的本年累计 SAP 明细、阶段 2 共享语义契约、证据校验、事务路由、工作任务隔离以及重跑幂等性。

- [ ] **步骤 2：记录后端范围、语义、工作任务及端到端证据**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-3-backend \
  --output artifacts/acceptance/phase-3/backend-command.json \
  -- bash -lc 'cd backend && pytest tests/unit/semantic tests/unit/workers/test_monthly_semantic_batch.py tests/integration/application/test_monthly_semantic_ingest_snapshot.py tests/integration/application/test_sap_voucher_monitor_transaction.py tests/integration/persistence/test_monthly_semantic_repository.py tests/integration/cases/test_welfare_donation_cases.py tests/integration/api/test_monthly_semantic_routes.py tests/integration/workers/test_monthly_semantic_batch_eager.py tests/evaluation/test_welfare_donation_golden.py tests/e2e/test_phase_3_monthly_semantic_flow.py -q --junitxml=../artifacts/acceptance/phase-3/backend.xml'
```

预期：以 0 退出；范围公式准确率为 100%，已知场景零漏检，试点召回率至少为 90%，正式门禁召回率至少为 95%，高置信度准确率至少为 80%。

- [ ] **步骤 3：记录前端及浏览器证据**

运行：

```bash
python3 scripts/acceptance/record_command.py \
  --name phase-3-web \
  --output artifacts/acceptance/phase-3/web-command.json \
  -- bash -lc 'cd web && npm test -- --run && npm run build && npx playwright test e2e/phase-3-welfare-donation.spec.ts'
```

预期：以 0 退出；福利费与捐赠支出筛选条件、SAP 证据、候选科目、置信度、版本及人工操作均可见，且受范围控制。

- [ ] **步骤 4：校验并提交阶段 3 证据**

运行：`python3 scripts/acceptance/verify_artifact.py artifacts/acceptance/phase-3/backend-command.json artifacts/acceptance/phase-3/backend.xml artifacts/acceptance/phase-3/web-command.json`

预期：以 0 退出。

```bash
git add artifacts/acceptance/phase-3
git commit -m "test(acceptance): approve welfare and donation monitors"
```

### 任务 5：实施并验收阶段 4 生产就绪能力

**文件：**
- 执行：`docs/superpowers/plans/2026-07-12-phase-4-governance-hardening-rollout.md`
- 校验：`Makefile`
- 校验：`artifacts/acceptance/phase-4/governance.xml`
- 校验：`artifacts/acceptance/phase-4/replay-report.json`
- 校验：`artifacts/acceptance/phase-4/capacity-report.json`
- 校验：`artifacts/acceptance/phase-4/rollback-report.json`
- 校验：`artifacts/acceptance/phase-4/uat-scorecard.json`

- [ ] **步骤 1：执行阶段 4 的全部任务并确认目标存在**

运行：`test -f Makefile && make -n verify-governance verify-release verify-capacity verify-rollback verify-migrations security-check uat`

预期：以 0 退出，且每个目标都展开为已检入的脚本；不得将未定义目标误判为业务门禁失败。

- [ ] **步骤 2：运行治理、发布、容量、迁移、安全、回退及 UAT 门禁**

运行：

```bash
make verify-governance
make verify-release
make verify-capacity COMPANY_FIXTURE=126
make verify-migrations
make security-check
make verify-rollback
make uat SNAPSHOT_SET=pilot-2026q2
```

预期：API/RLS/语义证据隔离和无外部索引断言通过；签名清单及重放校验通过；有效公司的成功率至少为 98%；基准配置在 24 小时内完成；有效公司的月度输出在 48 小时内就绪；回退可重复、可恢复；只有全部审批通过后，`production_ready=true` 才成立。

- [ ] **步骤 3：校验阶段 4 的全部制品及完整迁移登记表**

运行：

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

预期：以 0 退出；实际 Alembic 迁移链与登记表完全一致，直至 `0007`。

- [ ] **步骤 4：提交阶段 4 及最终登记证据**

```bash
git add artifacts/acceptance/phase-4 artifacts/acceptance/contract-registry-final.json
git commit -m "test(acceptance): approve production readiness"
```

### 任务 6：签署完整证据集并强制执行上线/回退顺序

**文件：**
- 修改：`docs/operations/acceptance-scorecard.md`
- 校验：`infra/runbooks/group-rollout.md`
- 校验：`infra/runbooks/rollback.md`
- 生成：`artifacts/acceptance/final-evidence-manifest.json`
- 生成：`artifacts/acceptance/final-evidence-manifest.sig`

跨阶段不变量：

| 契约 | 必需不变量 |
|---|---|
| 数据采集与血缘 | 每项结果都标识来源、期间、批次、记录行、校验结果及不可变快照。 |
| 税务主数据 | 税率、可弥补以前年度亏损及前三个完整年度平均税负率均按公司匹配，绝不推断。 |
| 金额/税率 | 中间过程使用 Decimal、最终按已批准的 `ROUND_HALF_UP` 舍入，比例阈值不依赖展示格式。 |
| 主体与范围 | 使用统一身份模型；API、RLS 及 PostgreSQL 语义证据采用相同的公司范围。 |
| 风险事项 | 指纹稳定、生命周期明确、重跑幂等、扩展兼容且历史可追溯。 |
| 语义判定 | 模型判定不依赖特定供应商，引用与科目经过校验，制品具备版本，最终由人工决策。 |
| 业务单据身份 | 仅允许精确关联，禁止任意挂接；后续 SAP 合并后仅保留一个有效风险暴露。 |
| 签名发布 | 应用及所有规则、模型和证据制品均可归因、可重放、经验证且可回退。 |

- [ ] **步骤 1：构建并校验最终签名证据清单**

运行：`make verify-release EVIDENCE_ROOT=artifacts/acceptance FINAL_MANIFEST=artifacts/acceptance/final-evidence-manifest.json`

预期：以 0 退出；清单及签名覆盖阶段 1–4 的命令结果、JUnit/结果文件、迁移登记表、评分卡、应用镜像、规则、提示词、模型配置、科目字典及案例库。

- [ ] **步骤 2：晋级前执行统一的可重复回退命令**

运行：`make verify-rollback CANDIDATE_MANIFEST=artifacts/acceptance/final-evidence-manifest.json`

预期状态序列：`PREFLIGHT_VERIFIED → TASKS_DRAINED_OR_REVOKED → EXPORTS_REVOKED → RESTORE_VERIFIED → PREVIOUS_RELEASE_DEPLOYED → CHECKSUMS_MATCHED → REPRESENTATIVE_RERUN_PASSED → RECOVERY_VERIFIED`。使用相同的已批准输入重复执行命令时，将跳过已验证阶段并返回相同的恢复结果。

- [ ] **步骤 3：按上线顺序执行，发现任何证据缺失即停止**

部署向后兼容的迁移/适配器，运行影子计算，与已批准的工作簿/案例对比，为试点公司启用季度风险，然后每个批次启用一个语义监测器及一组公司。在相同降级操作于恢复副本上成功前，绝不降级生产数据库。保留全部风险与审计历史。

- [ ] **步骤 4：记录审批并提交最终移交材料**

财务、税务、数据负责人、安全及运维人员使用证据哈希签署评分卡。签名、制品、阈值或回退检查点任一缺失，均阻止晋级。

```bash
git add docs/operations/acceptance-scorecard.md artifacts/acceptance/final-evidence-manifest.json artifacts/acceptance/final-evidence-manifest.sig
git commit -m "docs: approve phased tax monitoring rollout"
```

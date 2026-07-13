# 🧾 集团所得税风险监测平台

[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](CHANGELOG.md)
[![Technical Ready](https://img.shields.io/badge/technical__ready-true-success.svg)](docs/operations/acceptance-scorecard.md)
[![Evidence](https://img.shields.io/badge/evidence-LOCAL__SYNTHETIC-orange.svg)](artifacts/acceptance/phase-4/uat-scorecard.json)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](backend/pyproject.toml)
[![React](https://img.shields.io/badge/React-18-61DAFB.svg)](web/package.json)

面向 100 家以上集团公司的所得税风险监测平台，将汇算清缴时集中发现的问题前移到月度或季度自动监测。平台按季度检查所得税计提、累计税负和潜在纳税调增成本，按月识别业务招待费、福利费和公益性捐赠疑似错入明细，形成风险清单、证据链和改账建议。

当前已完成第一至第四阶段的离线可开发实现。现有验收证据范围为 `LOCAL_SYNTHETIC`：可以证明技术门禁通过，但不能替代真实公司试点、生产 KMS/HSM 签名和集团税务、数据、安全、运维四方批准。

---

## 🎯 监测能力

| 监测环节 | 频率 | 核心判断 | 主要输出 |
|---|---|---|---|
| 所得税计提准确性 | 季度 | 系统测算本季度应计提额与 SAP 实际计提额是否一致 | 差异公司、应计提额、实际计提额、计提差异 |
| 累计税负率异常 | 季度 | 当年累计税负率与线下表维护的前三个完整年度平均税负率偏离绝对值是否达到 5% | 异常公司、当年税负率、历史平均税负率、偏离度 |
| 潜在纳税调增成本 | 季度 | 其他应付款暂估与合思无票报销形成的潜在调增是否产生所得税成本 | 潜在调增金额、潜在应纳税额、潜在税务成本 |
| 纳税调增科目准确性 | 月度 | 业务招待费、福利费和公益性捐赠明细是否错入当前科目 | 疑似错入明细、证据、置信度、改账建议、复核任务 |

税率、可弥补以前年度亏损和前三个完整年度平均税负率均来自受控线下表，平台只按公司和有效期匹配，不自行判断税率或重新计算历史平均税负率。累计分红金额取 SAP 账上收到分红的金额。

季度潜在税务成本采用以下口径，保留加号语义：

```text
潜在调增金额
= 其他应付款暂估余额
+ 合思无票报销金额

本年累计潜在应计提所得税额
= max（损益表累计利润总额
- 累计分红金额
- 累计公允价值变动损益
- 可弥补以前年度亏损
+ 潜在调增金额, 0）
  × 适用税率

潜在税务成本
= 本年累计潜在应计提所得税额 - 本年累计应纳税额
```

完整公式、范围和异常处理见[功能设计说明书](docs/design/function/2026-07-12-group-income-tax-risk-monitoring-platform-function.md)。

## 🤖 Agent 工作流

### 季度监测

```text
SAP及线下税务主数据
→ 数据质量和有效期校验
→ 冻结公司快照集
→ 确定性公式逐公司测算
→ 计提/税负/潜在成本风险事项
→ 公司复核、处理与审计留痕
```

### 月度科目监测

```text
SAP凭证及OA/合思业务证据
→ 公司范围门禁和候选宽筛
→ 证据关联及最小必要字段读取
→ 业务招待费/福利费/捐赠专业Agent深判
→ 风险清单和改账建议
→ 公司财务复核、集团税务关闭
```

业务招待费支持三种证据路径：

- SAP 凭证能关联 OA 或合思单据时，以 SAP 凭证行为主记录并引用前置业务证据；
- OA 或合思单据不能关联 SAP 时，仍根据申请事由等字段独立判断，同时标记“待定位 SAP 凭证”；
- 仅有 SAP 凭证但找不到精确前置单据时，记录覆盖状态，不把未关联本身直接判定为错账。

福利费只有在“累计福利费－累计工资薪金×14%＞0”时进入明细检查；公益性捐赠只有在“累计公益性捐赠－累计利润总额×12%＞0”时进入明细检查。

## 🧪 代表性验收场景

### 季度标准公司

标准样例使用累计利润总额 1,000 万元、收到分红 100 万元、公允价值变动损益 50 万元、可弥补亏损 200 万元、税率 25%、累计营业收入 5,000 万元和历史平均税负率 9%。预期结果为：

- 本年累计应纳税额 162.50 万元；
- 本季度应计提 72.50 万元，SAP 实际计提 70 万元，少计提 2.50 万元；
- 当年累计税负率 3.25%，偏离度 -5.75%，触发税负偏低提示；
- 潜在调增 170 万元，潜在应纳税额 205 万元，潜在税务成本 42.50 万元。

### 月度典型科目

| 当前科目与摘要/事由 | 预期判断 | 建议 |
|---|---|---|
| 业务招待费：内部培训班会议餐 | 疑似错入 | 建议转职工教育经费 |
| 业务招待费：内部季度会议工作餐 | 疑似错入 | 建议转会议费 |
| 业务招待费：接待外部客户商务晚宴 | 当前科目合理 | 无需改账 |
| 未关联 SAP 的 OA 员工团建聚餐 | 疑似错入 | 定位 SAP 后建议转福利费 |
| 福利费：客户商务宴请 | 疑似错入 | 建议转业务招待费 |
| 福利费：员工培训费 | 疑似错入 | 建议转职工教育经费 |
| 福利费：员工年度体检 | 当前科目合理 | 无需改账 |
| 公益性捐赠：活动冠名及品牌露出 | 疑似错入 | 建议转广告宣传费 |
| 公益性捐赠：无对价公益捐赠且材料完整 | 当前科目合理 | 无需改账 |

本地验收还覆盖 105 家公司批次中 103 家成功、2 家因受控主数据问题阻断，以及 126 家公司容量场景。完整样例和阈值见[第四阶段验收评分卡](docs/operations/acceptance-scorecard.md)。

## 📊 当前技术验收

| 指标 | 本地合成证据结果 |
|---|---:|
| 公式准确率 | 100% |
| 可追溯率 | 100% |
| 主数据缺陷阻断率 | 100% |
| 有效公司成功率 | 100% |
| 语义召回率 | 96% |
| 高置信度准确率 | 82% |
| 已知典型案例漏检 | 0 |
| 容量验收 | 126 家公司 |

当前自动评分为 `technical_ready=true`、`production_ready=false`。任何真实批准或生产证据缺失时，发布决策必须保持 `NO-GO`。

## 📁 仓库结构

```text
.
├── backend/                  # FastAPI、PostgreSQL、Celery、公式与语义Agent
├── web/                      # React季度驾驶舱、月度风险、导出和运维页面
├── infra/                    # Compose、可观测性、验证脚本和中文运行手册
├── docs/
│   ├── design/              # 架构、功能与详细设计
│   ├── operations/          # 验收评分卡、数据负责人清单和用户培训
│   └── superpowers/         # 经确认的设计规格与实施计划
├── artifacts/acceptance/    # 本地验收生成的可复核证据
├── Makefile                 # 统一验证、发布和回滚入口
├── CHANGELOG.md             # 阶段版本历史
└── README.md
```

详细模块边界见[架构设计说明书](docs/design/architecture/2026-07-12-group-income-tax-risk-monitoring-platform-architecture.md)。

## 🚀 本地启动

准备环境并启动数据服务：

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d postgres redis database-roles
export MIGRATION_DATABASE_URL='postgresql+psycopg://tax_risk_owner:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export DATABASE_URL='postgresql+psycopg://tax_risk_app:replace-for-local-application-only@127.0.0.1:5432/tax_risk'
export TEST_DATABASE_URL="$MIGRATION_DATABASE_URL"
export REDIS_URL='redis://127.0.0.1:6379/0'
```

安装后端和前端依赖：

```bash
uv venv backend/.venv
uv pip install --python backend/.venv/bin/python -e 'backend[dev]'
cd web && npm ci && cd ..
```

迁移数据库并启动完整本地技术栈：

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
```

Web 默认地址为 `http://127.0.0.1:8080/`，API 为 `http://127.0.0.1:8000/`。本地固定身份只能用于隔离开发机；生产必须关闭开发身份并接入批准的 IdP。

## ✅ 验证

```bash
make test-backend
make test-web
make verify-governance
make security-check
make verify-migrations
make verify-release
make verify-capacity COMPANY_FIXTURE=126
make verify-rollback
make uat SNAPSHOT_SET=pilot-2026q2
```

所有脚本使用失败即退出，并检查 JSON 或 JUnit 制品是否存在且满足阈值。季度外部技术栈浏览器测试需要先注入唯一的 105 家公司验收数据；未配置真实外部 E2E 时会明确标记为跳过，不能计入生产证据。

## 📈 监控与告警

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS http://127.0.0.1:8000/metrics
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f \
  api worker-quarterly worker-business-entertainment worker-monthly-semantic \
  worker-exports web
```

运维驾驶舱分别展示数据错误、技术失败和税务风险。`SnapshotSet.published_at` 是唯一数据就绪时间；公司成功提交完整结果后才写入 `company_output_ready_at`。数据或主数据阻断不得输出“无风险”，技术失败不得写入虚假输出就绪时间。

## 🔐 发布、升级与回滚

先运行治理、迁移、重放、容量、回滚和 UAT 门禁，再创建发布候选。规范清单锁定镜像摘要、Git 提交、迁移头、规则、提示词、模型适配器配置、科目字典、案例库和评估/重放报告摘要。

本地 `make verify-release` 只使用单次临时 Ed25519 密钥证明链路，制品标记为 `CI_EPHEMERAL_NOT_FOR_PRODUCTION`。试点和生产必须通过 OIDC 工作负载身份调用允许清单中的 KMS/HSM 密钥，并在下载制品后重新验签。完整步骤见[签名发布操作手册](infra/runbooks/release.md)。

升级数据库前必须备份并在隔离副本执行：

```bash
make verify-migrations
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini current
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini check
```

严禁在主数据库或唯一副本上直接执行降级。备份、恢复与回滚命令见[基础设施运维手册](infra/README.md)和[回滚与恢复操作手册](infra/runbooks/rollback.md)。

```bash
make verify-release
make verify-rollback
```

回滚会验证签名、排空或撤销任务、撤销导出、恢复隔离备份、演练迁移降级/重升、部署上一清单、比较校验和并重跑代表公司。相同批准输入可从检查点续跑，不重复恢复、部署或创建风险事项。

## 🧭 生产准入与推广

真实试点必须使用冻结公司快照，补齐集团税务、数据、安全和运维四类实名批准，并使用生产 KMS/HSM 签名。相关入口：

- [试点 UAT 操作手册](infra/runbooks/pilot-uat.md)
- [集团分批推广手册](infra/runbooks/group-rollout.md)
- [数据负责人检查清单](docs/operations/data-owner-checklist.md)
- [用户培训手册](docs/operations/user-training.md)
- [第四阶段验收评分卡](docs/operations/acceptance-scorecard.md)

## 📜 版本历史

第一至第四阶段的重要变更见 [CHANGELOG](CHANGELOG.md)。

# 集团所得税风险监测平台

本平台面向 100 家以上集团公司，将所得税汇算清缴集中发现的问题前移到月度和季度自动监测。季度监测所得税计提准确性、累计税负率偏离和潜在纳税调增税务成本；月度识别业务招待费、福利费和公益性捐赠疑似错入明细，输出风险清单和改账建议。

当前仓库已完成第一至第四阶段的离线可开发实现：确定性公式、三类月度语义 Agent、统一风险事项、公司权限隔离、不可变审计、安全导出、企业模型网关、运维监控、126 家容量验收、签名发布门禁和可续跑回滚演练。由于尚未接入真实数据接口和真实试点数据，当前证据范围为 `LOCAL_SYNTHETIC`；它可以证明技术门禁，但不能代替真实试点、生产 KMS/HSM 签名和业务批准。

## 目录

- `backend/`：FastAPI、PostgreSQL、Celery、公式与语义 Agent、发布/回滚控制。
- `web/`：季度驾驶舱、月度风险、导出和运维页面。
- `infra/`：Compose、可观测性配置、验证脚本和中文操作手册。
- `docs/design/`：架构、功能和详细设计。
- `docs/operations/`：验收评分卡、数据负责人清单和用户培训。
- `artifacts/acceptance/`：本地验收命令生成的可复核证据。

## 本地启动

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

## 验证命令

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

所有脚本使用失败即退出，并验证其 JSON 或 JUnit 制品存在且满足阈值。季度外部技术栈浏览器测试需要先完成唯一的 105 家公司数据注入；本地未配置真实外部 E2E 时会明确标记为跳过，不能计入生产证据。

## 监控与告警

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS http://127.0.0.1:8000/metrics
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f \
  api worker-quarterly worker-business-entertainment worker-monthly-semantic \
  worker-exports web
```

运维驾驶舱分别展示数据错误、技术失败和税务风险。`SnapshotSet.published_at` 是唯一数据就绪时间；公司成功提交完整结果后才写入 `company_output_ready_at`。数据或主数据阻断不得输出“无风险”，技术失败不得写入虚假输出就绪时间。

## 发布与升级

先运行全部治理、迁移、重放、容量、回滚和 UAT 门禁，再创建候选。规范清单锁定镜像摘要、Git 提交、迁移头、规则、提示词、模型适配器配置、科目字典、案例库和评估/重放报告摘要。

本地 `make verify-release` 只使用单次临时 Ed25519 密钥证明链路，制品标记为 `CI_EPHEMERAL_NOT_FOR_PRODUCTION`。试点和生产必须通过 OIDC 工作负载身份调用允许清单中的 KMS/HSM 密钥，并在下载制品后重新验签。完整步骤见 [签名发布操作手册](infra/runbooks/release.md)。

升级数据库前必须备份并在隔离副本执行：

```bash
make verify-migrations
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini current
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini check
```

严禁在主数据库或唯一副本上直接执行降级。

## 备份、恢复与回滚

备份和恢复命令见 [基础设施运维手册](infra/README.md)。恢复必须进入一次性隔离目标，对比源数据、快照、风险事项和关键金额校验和，再重跑一家代表性公司。

```bash
make verify-release
make verify-rollback
```

回滚脚本依次验证签名、排空或撤销任务、撤销导出、恢复隔离备份、演练迁移降级/重升、部署上一清单、比较校验和、代表公司重跑并记录恢复决定。相同批准输入可从检查点续跑且不会重复恢复、部署或创建风险事项。试点/生产操作要求批准变更号、申请人与批准人分离及平台命令证据，详见 [回滚与恢复操作手册](infra/runbooks/rollback.md)。

## 生产准入状态

[第四阶段验收评分卡](docs/operations/acceptance-scorecard.md)规定了全部阈值和证据。当前本地合成验证预期为：

- `technical_ready=true`：自动化代码、金标、容量、安全、迁移和回滚门禁通过；
- `production_ready=false`：仍缺真实冻结快照 UAT、生产 KMS/HSM 签名以及集团税务、数据、安全和运维四方实名批准。

在上述真实证据补齐之前，发布决策必须保持 `NO-GO`。

# 第一阶段基础设施运维手册

本手册用于从仓库根目录运维具备生产形态的第一阶段技术栈。Compose 拓扑包含 PostgreSQL、Redis、
一次性数据库迁移服务、API、季度任务工作进程以及 Web/Nginx 服务，不包含语义 Agent 或模型服务。

## 前置条件

- 安装带有 Docker Compose v2 的 Docker Engine
- 安装用于 API 操作的 `curl` 和 `jq`
- 安装 Python 3.12，并准备后端开发环境，用于验收测试
- 安装 Node.js 22，用于浏览器验收

除非命令明确切换目录，否则以下所有命令均应在仓库根目录执行。

## 配置

创建本地验收环境，并验证展开后的 Compose 配置：

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml config --quiet
```

`infra/env.example` 包含固定且仅供本地使用的开发 Principal。Nginx 在服务端注入其签名请求头；
HMAC 密钥绝不会进入浏览器构建产物。不得在隔离的开发机以外复用这些值。

对于每个部署环境，必须通过经批准的密钥存储替换本地数据库密码和 URL，设置
`ENVIRONMENT=production`，禁用并移除所有 `DEVELOPMENT_PRINCIPAL_*` 值，并将经批准的生产 IdP
验证器注入 FastAPI 应用组合。缺少该验证器时，生产环境必须以 HTTP 401 拒绝访问；启用开发标志
不能绕过生产环境防护。

## 启动

构建并启动完整技术栈：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
docker compose --env-file infra/.env -f infra/docker-compose.yml ps -a
```

`migrate` 必须以退出码 0 完成。PostgreSQL、Redis、API、`worker-quarterly` 和 Web 必须报告健康状态。
PostgreSQL、Redis、API 和 Web 仅向宿主机回环地址发布端口。

## 健康检查

检查服务状态及各健康检查端点：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml ps -a
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T redis redis-cli ping
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T worker-quarterly celery -A tax_risk.workers.celery_app:celery_app inspect ping --timeout 5
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8080/healthz
```

HTTP 健康检查端点仅检查进程健康状态。系统达到可运行状态还要求数据库迁移成功，并且上述
PostgreSQL、Redis 和季度任务工作进程检查全部通过。

## 生产上线准入

在以下每一项均已完整登记至受控发布记录，且最终决策明确为 `GO` 之前，第一阶段技术栈
**严禁部署至生产环境**。本地静态测试通过、镜像构建成功或进程内 `eager-worker` 测试通过，
均不能视为满足以下门禁。

| 门禁 | 必须记录的证据 | 取得证据前的状态 |
|---|---|---|
| 字段映射签字确认 | 经批准的 SAP/合思字段映射、正负号约定、数据源负责人、复核人及批准日期 | `PENDING` |
| 金额精度签字确认 | 各公司币种与金额精度映射、`ROUND_HALF_UP` 确认、复核人及批准日期 | `PENDING` |
| 105家公司部署服务 E2E | `E2E_SEED_TOKEN`、监测 `run_id`、执行时间戳、环境/镜像标识，以及精确结果 `105 requested / 103 succeeded / 2 blocked / 0 failed` | `PENDING` |
| 浏览器验收 | `E2E_STANDARD_COMPANY_CODE`、Playwright 结果、跟踪或报告位置、执行时间戳，以及公式抽屉与持久化 API 值一致的确认记录 | `PENDING` |
| 业务批准 | 业务批准人、批准日期、关联验收报告，以及明确的 `GO` 或 `NO-GO` 决策 | `PENDING` |

验收报告必须明确说明：外部 E2E 直接访问数据库的用途，仅限于解析固定的已发布规则，以及注入两项
已有文档记录的发布后漂移条件。这些仅供测试使用的操作不属于生产运维规程。任何一项失败、缺失、
过期或未经签字确认，发布状态都必须保持为 `NO-GO`。

## 数据库迁移

Compose 会在启动 API 和工作进程前执行 `alembic upgrade head`。如需显式执行一次性迁移并检查结果：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml run --rm migrate
docker compose --env-file infra/.env -f infra/docker-compose.yml logs --no-color migrate
```

严禁降级主数据库。按照后端手册的说明，任何降级测试都只能在由已验证备份创建的一次性数据库上执行。

## 数据准备

系统不提供独立的部署数据注入 CLI。`backend/tests/e2e/seed_quarterly_scenario.py` 是测试辅助程序，
不是命令。外部 E2E 测试会针对运行中的技术栈调用公共的数据采集、tax-master、快照、运行批次、风险清单和
检测 API。该测试仅为定位已发布规则及注入两项特意设置的发布后漂移条件而直接连接数据库。

使用唯一标识注入外部105家公司验收数据，并执行真实的 API/Redis/Celery 流程：

```bash
export E2E_BASE_URL=http://127.0.0.1:8000
export E2E_DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export E2E_DEV_PRINCIPAL_SECRET='local-only-tax-risk-development-secret-do-not-use-in-production'
export E2E_SEED_TOKEN="run$(date +%Y%m%d%H%M%S)"
export E2E_STANDARD_COMPANY_CODE="E2E-${E2E_SEED_TOKEN}-000"
export E2E_WORKER_TIMEOUT_SECONDS=300
backend/.venv/bin/pytest -q -s backend/tests/e2e/test_quarterly_api_worker_flow.py
```

令牌必须唯一，长度为 6 至 32 个字符，且只能包含字母、数字、`_` 或 `-`。数据库 URL 必须指向
API 和工作进程使用的同一数据库。测试使用不同的制单人和复核人主体标识对所有控制面调用签名，调用真实的
消息代理和工作进程，预期结果必须精确为 105 家公司已请求、103 家公司成功、2 家公司阻断、0 家公司失败，
并输出 Playwright 所需的标准公司代码。

`SnapshotSet.published_at` 是唯一权威的数据就绪时间戳。上传时间、验证时间、工作进程执行时间和
驾驶舱读取时间均不得替代该时间戳。

## 提交

对于单独准备的已发布快照集，应通过本地 Web 代理提交。本地 Nginx 服务会添加验收身份；
生产请求必须改由注入的 IdP 验证器完成身份认证。

```bash
export WEB_URL=http://127.0.0.1:8080
export SNAPSHOT_SET_ID='<已发布快照集UUID>'
export RULE_VERSION_ID='<已发布规则版本UUID>'
RUN_RESPONSE=$(curl -fsS -X POST "$WEB_URL/api/v1/quarterly-runs" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg snapshot_set_id "$SNAPSHOT_SET_ID" \
    --arg rule_version "$RULE_VERSION_ID" \
    '{fiscal_year: 2026, quarter: 2, snapshot_set_id: $snapshot_set_id, rule_version: $rule_version}')")
printf '%s\n' "$RUN_RESPONSE" | jq .
export RUN_ID=$(printf '%s\n' "$RUN_RESPONSE" | jq -r .run_id)
```

系统仅接受以原子方式发布的快照集，以及已发布且已批准的季度规则版本。

## 检查

轮询已持久化的运行批次，然后检查驾驶舱和风险事项：

```bash
curl -fsS "$WEB_URL/api/v1/quarterly-runs/$RUN_ID" | jq .
curl -fsS "$WEB_URL/api/v1/dashboard/quarterly?fiscal_year=2026&quarter=2" | jq .
curl -fsS "$WEB_URL/api/v1/risk-cases?fiscal_year=2026&quarter=2&page=1&page_size=100" | jq .
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres psql -U tax_risk -d tax_risk -c "SELECT status, count(*) FROM monitoring_run_company WHERE run_id = '$RUN_ID' GROUP BY status ORDER BY status;"
```

运行批次终态为 `SUCCEEDED`、`PARTIAL_SUCCESS` 或 `FAILED`。`BLOCKED` 是公司层面的数据或控制结果，
不得将其作为技术失败进行重试。

## 重试

Celery 会自动重试可重试的公司任务。第一阶段不提供公共的手工重试 API。如获授权的运维人员必须将终态运行批次
重新入队，以下受控内部操作将选择全部且仅选择状态为 `FAILED` 的公司记录；该操作绝不会重新运行
`SUCCEEDED` 或 `BLOCKED` 公司：

```bash
export RUN_ID='<终态运行批次UUID>'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T api python - "$RUN_ID" <<'PY'
import sys
from uuid import UUID

from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.workers.celery_app import celery_app
from tax_risk.workers.quarterly_batch import build_quarterly_batch_canvas

plan = QuarterlyBatchService(UnitOfWork).retry_failed(run_id=UUID(sys.argv[1]))
if plan.run_company_ids:
    build_quarterly_batch_canvas(
        app=celery_app,
        run_id=plan.run_id,
        run_company_ids=plan.run_company_ids,
    ).apply_async()
print({
    "run_id": str(plan.run_id),
    "requeued_run_company_ids": [str(value) for value in plan.run_company_ids],
})
PY
```

必须在运维日志中记录操作人、变更单号、运行批次 ID，以及返回的公司任务 ID。返回空列表表示该运行批次
不存在符合本操作条件的 `FAILED` 公司。

## 日志

读取实时日志，或导出不含 ANSI 颜色代码的支持包：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api worker-quarterly web
mkdir -p artifacts/logs
docker compose --env-file infra/.env -f infra/docker-compose.yml logs --no-color --since=24h postgres redis migrate api worker-quarterly web > artifacts/logs/phase1-stack.log
```

不得将已上传的源数据、自由文本证据、数据库 URL、`Principal` 请求头或密钥写入工单或共享日志包。

## 备份与恢复

创建自定义格式备份，并且只能将其恢复至一次性验证数据库：

```bash
mkdir -p backups
export BACKUP_FILE="backups/tax_risk_$(date +%Y%m%d_%H%M%S).dump"
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP_FILE"
export TEST_DB=tax_risk_restore_test
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$TEST_DB" && createdb -U "$POSTGRES_USER" "$TEST_DB"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$TEST_DB" --exit-on-error' < "$BACKUP_FILE"
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'psql -U "$POSTGRES_USER" -d "$TEST_DB" -Atc "SELECT version_num FROM alembic_version"'
docker compose --env-file infra/.env -f infra/docker-compose.yml exec -T -e TEST_DB="$TEST_DB" postgres sh -c 'dropdb -U "$POSTGRES_USER" "$TEST_DB"'
```

本手册有意不提供覆盖主数据库的恢复方法。生产恢复和回滚必须经过已批准的变更流程，并具备隔离的目标环境、
校验和验证，以及经过演练的应用和数据库迁移回滚方案。

## 停止

停止技术栈，同时保留 PostgreSQL 和 Redis 数据卷：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml down
```

仅当环境为一次性本地环境，且已确认不再需要任何证据或审计历史时，才可删除数据卷：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml down --volumes
```

第一阶段使用确定性公式，不需要模型、LLM 或语义 Agent 凭据。

# 第一阶段后端

后端基于 Python 3.12、FastAPI、SQLAlchemy/Alembic、PostgreSQL 和 Celery 构建，
用于执行集团所得税季度确定性监测。

## 环境准备

在仓库根目录创建开发环境：

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip setuptools
backend/.venv/bin/pip install --constraint backend/requirements.lock -e 'backend[dev]'
backend/.venv/bin/python -m pip check
```

容器构建同样使用 `backend/requirements.lock` 约束依赖版本。运行时依赖或开发依赖发生变化时，
必须同步更新 `pyproject.toml` 和锁定文件。

## 数据库

启动仅监听本机回环地址的数据存储服务，并让宿主机上的后端命令连接 PostgreSQL：

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d postgres redis
export DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export REDIS_URL='redis://127.0.0.1:6379/0'
```

应用容器使用 Compose 服务名 `postgres` 和 `redis`；宿主机测试使用 `127.0.0.1`。
在任何共享环境或部署环境中，都必须替换全部仅供本地使用的凭据。

## 数据库迁移

检查并升级已配置的数据库：

```bash
cd backend
.venv/bin/alembic current
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

严禁对主数据库或数据库的唯一副本执行 `alembic downgrade`。将已验证的备份恢复至一次性数据库后，
仅可在该副本上演练降级：

```bash
cd backend
export DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk_restore_test'
.venv/bin/alembic downgrade -1
.venv/bin/alembic upgrade head
```

验证迁移路径后，应销毁演练数据库。只有同一降级操作已在恢复副本上成功完成，且经批准的回滚流程明确授权后，
才允许执行生产降级。

## API

后端开发时，可直接运行 API：

```bash
cd backend
.venv/bin/uvicorn tax_risk.main:create_app --factory --host 127.0.0.1 --port 8000
```

已实现的端点如下：

- `GET /health`
- `POST /api/v1/ingest-batches`、`POST /api/v1/ingest-batches/{id}/files` 和
  `GET /api/v1/ingest-batches/{id}`
- `POST /api/v1/tax-master/import`、`POST /api/v1/tax-master/{id}/approve` 和
  `GET /api/v1/tax-master/{company_code}`
- `POST /api/v1/snapshots/validate`、`POST /api/v1/snapshots/{id}/publish` 和
  `POST /api/v1/snapshot-sets`
- `POST /api/v1/quarterly-runs` 和 `GET /api/v1/quarterly-runs/{id}`
- `GET /api/v1/dashboard/quarterly`、`GET /api/v1/risk-cases`、
  `POST /api/v1/risk-cases/{id}/actions` 和 `GET /api/v1/detections/{id}`

所有 ingest-batch、tax-master 和 snapshot 控制面路由均要求 `group-tax` 管理角色。
季度监测、风险、驾驶舱和检测端点还会在服务端 SQL 中强制校验 `Principal` 的角色和公司权限范围。
旧版传输字段 `uploaded_by` 和 `reviewed_by` 仅为保持接口兼容而接收；持久化的制单人和复核人身份取自
`Principal.subject`，请求正文不能指定这些身份。只有同时满足 `ENVIRONMENT=development` 和
`DEVELOPMENT_PRINCIPAL_ENABLED=true` 时，系统才接受已签名的开发请求头。生产环境必须注入 IdP
验证器，否则返回 HTTP 401；健康检查端点保持公开，供编排系统使用。

## 季度任务

在本地运行真实的季度任务队列：

```bash
cd backend
.venv/bin/celery -A tax_risk.workers.celery_app:celery_app worker --queues=quarterly --concurrency=4 --loglevel=INFO
```

对应的 Compose 命令如下：

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d worker-quarterly
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f worker-quarterly
```

任务仅携带可持久化 ID。工作进程会从 PostgreSQL 重新加载冻结快照、tax-master 版本和规则版本；
系统采用 JSON 序列化、延迟确认、工作进程丢失时拒绝确认、有限任务超时，以及按公司隔离的重试机制。

## 重试

Celery 自动重试仅适用于单家公司执行失败。手工批量重试通过应用操作
`QuarterlyBatchService.retry_failed(run_id=...)` 执行：仅终态为 `PARTIAL_SUCCESS` 或 `FAILED`
的运行批次可以重试，并且会重置全部且仅重置状态为 `FAILED` 的公司记录。该操作绝不会重新计算
`SUCCEEDED` 公司，也绝不会重试因数据或控制原因处于 `BLOCKED` 状态的公司。

第一阶段不将该操作开放为公共 HTTP 端点。获授权的运维人员必须使用 `infra/README.md` 中受控的内部命令，
记录命令返回的公司任务 ID，并由正常的 Celery `canvas` 重新汇总运行结果。

## 测试

在仓库根目录执行后端质量门禁：

```bash
backend/.venv/bin/pytest backend/tests -q
backend/.venv/bin/ruff check backend/src backend/tests infra/tests
backend/.venv/bin/mypy backend/src
```

针对本机回环地址上的 PostgreSQL 服务执行隔离的确定性 E2E 契约测试：

```bash
backend/.venv/bin/pytest -q backend/tests/e2e/test_quarterly_standard_scenario.py backend/tests/e2e/test_quarterly_eager_worker_contract.py
```

只有在 Compose API、Redis 和真实季度任务工作进程均健康后，才可执行部署服务 E2E 测试：

```bash
export E2E_BASE_URL=http://127.0.0.1:8000
export E2E_DATABASE_URL='postgresql+psycopg://tax_risk:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export E2E_DEV_PRINCIPAL_SECRET='local-only-tax-risk-development-secret-do-not-use-in-production'
export E2E_SEED_TOKEN="run$(date +%Y%m%d%H%M%S)"
export E2E_STANDARD_COMPANY_CODE="E2E-${E2E_SEED_TOKEN}-000"
export E2E_WORKER_TIMEOUT_SECONDS=300
backend/.venv/bin/pytest -q -s backend/tests/e2e/test_quarterly_api_worker_flow.py
```

此外部测试使用不同的 `group-tax` 制单人和复核人主体标识对每个控制面请求签名，通过 HTTP 注入种子数据，
并实际调用消息代理和工作进程。该测试不是进程内 `eager-worker` 契约测试。

## 数值契约

- 必须从字符串构造 `Decimal` 值；会计金额和税务金额严禁经过二进制浮点数处理。
- 货币输出必须按照受控币种和金额精度，并使用 `ROUND_HALF_UP` 进行量化。
- 输入和证据必须保留数据库的完整精度。API 中的 Decimal 值使用精确字符串表示，并在适用时附带币种和精度。
- 税率使用小数而非百分数表示：`0.25` 表示 25%，预警阈值 `0.05` 表示五个百分点。
- 公式重放必须使用冻结快照、tax-master 版本、规则版本及已持久化的公式代入值。浏览器不得重新计算税务结果。

## 第一阶段边界

第一阶段实现受控数据采集、不可变的已发布快照集、三项已批准的季度确定性检查、可审计风险事项、
受权限范围控制的 API，以及季度驾驶舱。`SnapshotSet.published_at` 是唯一的数据就绪时间戳。

第一阶段不包含语义 Agent、LLM 适配器、模型服务器、提示词、向量索引或模型凭据。业务招待费、福利费和
捐赠支出的语义分类属于后续阶段，严禁引入当前运行环境或工作进程队列。

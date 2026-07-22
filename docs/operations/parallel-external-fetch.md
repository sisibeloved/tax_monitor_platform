# 并行取数与 Redis 缓存运维

## 运行模型

生产 API 使用一个进程内共享线程池并行执行相互独立的外部取数请求。默认全局上限为 12，利润表、科目发生额表、科目余额表和汇算清缴相关科目明细各自最多并发 4 个请求。全局上限控制本进程资源，数据源上限保护上游接口；批量返回顺序始终与输入顺序一致。

现有单笔 DGC 导入端点也经过同一协调器，因此自动获得缓存、防击穿和重试能力。季度监测计算继续读取已经发布的冻结快照，不会在公式事务中调用外部接口。

## 缓存语义

- Redis 键只包含命名空间、数据源代码和规范化参数的 SHA-256，不包含公司代码、业务参数或密钥。
- 缓存载荷保存版本、来源 checksum、取数时间和载荷 SHA-256；读取时逐项校验，损坏条目会删除并使当前请求失败。
- `Decimal` 使用带类型标记的十进制字符串序列化，不转换为二进制浮点数。
- 非空成功结果默认缓存 900 秒；真实空结果按 `REAL/NO_DATA` 缓存 60 秒，不能触发模拟回退。仅专用科目发生额查询在公司、年度和科目 `6801010000` 作用域完整时，将空结果解释为该公司未计提，并在适配层生成两项有来源证据的 `0`；缓存层本身不改变空结果语义。
- 缓存未命中时使用 Redis `SET NX EX` 取得分布式锁。锁持有者定期通过 compare-and-expire Lua 脚本续租，并通过 compare-and-delete Lua 脚本释放；等待者只轮询缓存，超时后失败，不会自行冲击上游。
- 真实接口错误不会使用过期缓存或模拟数据兜底。

## 重试与失败

只重试传输层错误及 HTTP `408/429/5xx`，默认最多 3 次，采用指数退避和抖动。鉴权失败、TLS 证书失败、响应字段错误、分页错误和资源上限错误不重试。批量中任何真实数据源失败都会使整个批次失败，已成功的结果不会被拼成不完整业务输入。

## 配置

生产 Compose 默认设置：

```dotenv
EXTERNAL_FETCH_ENABLED=true
EXTERNAL_FETCH_CACHE_ENABLED=true
EXTERNAL_FETCH_MAX_WORKERS=12
EXTERNAL_FETCH_SOURCE_CONCURRENCY__DGC_SAP_PROFIT=4
EXTERNAL_FETCH_SOURCE_CONCURRENCY__DGC_SAP_TRIAL_BALANCE=4
EXTERNAL_FETCH_SOURCE_CONCURRENCY__DGC_SAP_ACCOUNT_BALANCE=4
EXTERNAL_FETCH_SOURCE_CONCURRENCY__DGC_SAP_DIVIDEND_DETAIL=4
EXTERNAL_FETCH_CACHE_TTL_SECONDS=900
EXTERNAL_FETCH_EMPTY_CACHE_TTL_SECONDS=60
EXTERNAL_FETCH_LOCK_TTL_SECONDS=300
EXTERNAL_FETCH_LOCK_WAIT_SECONDS=305
EXTERNAL_FETCH_RETRY_MAX_ATTEMPTS=3
```

生产环境启用并行取数时，应用会强制要求 Redis 缓存同时启用。修改并发数前必须同时核对 DGC 网关配额、API 容器副本数和数据库导入吞吐；实际全局上游并发约为“每实例上限 × API 实例数”，不能只看单实例配置。

## 监控与告警

`/metrics` 暴露以下低基数指标，不包含公司或查询参数：

- `tax_risk_external_fetch_total{source,provenance,result}`
- `tax_risk_external_fetch_retry_total{source,error_code}`
- `tax_risk_external_fetch_failure_total{source,error_code}`
- `tax_risk_external_fetch_duration_seconds{source,provenance}`

建议对失败率、重试率、P95 时延和 Redis 可用性告警。`CACHE` 命中率持续为 0 时，应检查 TTL、请求参数规范化和 Redis 数据淘汰策略；`NO_DATA` 激增时，应先核对上游筛选条件，不得直接按 0 入账。

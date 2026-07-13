# 第四阶段验收评分卡

## 门禁定义

| 门禁 | 阈值 | 本地证据路径 | 真实试点要求 |
|---|---:|---|---|
| 公式准确率 | 100% | `replay-report.json` | 财务与税务抽样复核 |
| 可追溯率 | 100% | `replay-report.json` | 原始来源、快照、版本和公式完整 |
| 主数据缺陷阻断率 | 100% | `replay-report.json` | 缺陷公司不得输出安全结论 |
| 有效公司成功率 | ≥98% | `capacity-report.json` | 真实批次公司终态 |
| 生产召回率 | ≥95% | `replay-report.json` | 冻结金标与已知案例 |
| 高置信度准确率 | ≥80% | `replay-report.json` | 人工复核样本 |
| 已知案例漏检 | 0 | `replay-report.json` | 业务招待、福利、捐赠典型案例 |
| 月度交付 | ≤48小时 | `capacity-report.json` | 数据就绪至公司输出就绪 |
| 授权与隔离 | 全部通过 | `governance.xml`、`security.json` | API、RLS、证据读取对抗验证 |
| 外部语义索引 | 未配置 | `security.json` | 配置审查 |
| 审计不可变 | 通过 | `security.json` | 数据库更新/删除拒绝 |
| 签名 | 验证通过 | `signed-manifest.json` | 生产必须为 KMS/HSM |
| 恢复与回滚 | 通过 | `rollback-report.json` | 批准变更、隔离恢复和代表公司重跑 |

## 自动评分

执行 `make uat SNAPSHOT_SET=pilot-2026q2` 生成 `artifacts/acceptance/phase-4/uat-scorecard.json`。任何指标、证据引用或批准角色缺失时，`production_ready` 必须为 `false`。

当前无真实接口和真实试点数据时，证据范围标记为 `LOCAL_SYNTHETIC`。此范围可以得到 `technical_ready=true`，但不能代替生产批准，也不能得到 `production_ready=true`。真实试点必须将范围设为 `PILOT_PRODUCTION`，提供税务、数据、安全和运维四类实名批准，并使用 KMS/HSM 签名清单。

## 批准记录

| 角色 | 批准人 | 时间 | 证据/变更号 |
|---|---|---|---|
| 集团税务负责人 | 待真实试点填写 |  |  |
| 数据负责人 | 待真实试点填写 |  |  |
| 安全负责人 | 待真实试点填写 |  |  |
| 运维负责人 | 待真实试点填写 |  |  |

## 本地证据链接

- [后端 JUnit](../../artifacts/acceptance/backend.xml)
- [前端 Playwright JSON](../../artifacts/acceptance/web-test-results/results.json)
- [治理 JUnit](../../artifacts/acceptance/phase-4/governance.xml)
- [安全检查](../../artifacts/acceptance/phase-4/security.json)
- [迁移检查](../../artifacts/acceptance/phase-4/migrations.json)
- [重放报告](../../artifacts/acceptance/phase-4/replay-report.json)
- [签名清单](../../artifacts/acceptance/phase-4/signed-manifest.json)
- [126 家容量报告](../../artifacts/acceptance/phase-4/capacity-report.json)
- [回滚报告](../../artifacts/acceptance/phase-4/rollback-report.json)
- [UAT 评分卡](../../artifacts/acceptance/phase-4/uat-scorecard.json)

以上是本地生成的验收制品，其中签名和业务数据均为非生产范围。真实试点须将受控制品复制到批准的证据库，记录不可变对象版本、保留策略和四方批准，不得只引用开发工作区文件。

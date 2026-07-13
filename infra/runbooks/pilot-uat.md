# 试点用户验收手册

## 顺序和范围

依次选择内部测试公司、覆盖盈利/亏损及不同关联模式的代表公司，再执行一次完整季度并行运行。试点使用冻结快照集，不在验收过程中修改规则、提示词、案例库、模型适配器或科目字典版本。

## 执行命令

```bash
make verify-governance
make security-check
make verify-migrations
make verify-release
make verify-capacity COMPANY_FIXTURE=126
make verify-rollback
make uat SNAPSHOT_SET=pilot-2026q2
```

本地无真实数据时输出范围为 `LOCAL_SYNTHETIC`，只能证明技术门禁，`production_ready` 必须保持 `false`。真实试点需提供：

```bash
export UAT_EVIDENCE_SCOPE=PILOT_PRODUCTION
export UAT_APPROVALS_JSON='{"tax_owner":"...","data_owner":"...","security_owner":"...","operations_owner":"..."}'
export UAT_REQUIRE_PRODUCTION_READY=true
make uat SNAPSHOT_SET=真实冻结快照集ID
```

## 双重复核和签字

财务人员逐项复核账面金额、建议改账科目和凭证；税务人员复核公式、纳税调增判断和风险结论。数据负责人确认源数据、快照和对账；安全负责人确认授权/RLS/语义证据隔离；运维负责人确认容量、恢复和回滚。四类批准缺一不可，且不得由自动化账号代签。

## 通过标准

公式准确率、可追溯率、主数据缺陷阻断率均为 100%；有效公司成功率至少 98%；生产召回率至少 95%；高置信度准确率至少 80%；已知案例零漏检；月度交付不超过 48 小时；签名、迁移、安全、恢复和回滚全部有可验证证据。


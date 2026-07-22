# 1 详细设计说明书

## 1.1 产品版本&密级

- 产品版本：V0.1。
- 文档版本：V1.1。
- 文档密级：集团内部。

## 1.2 拟制信息

- 拟制日期：2026-07-12。
- 拟制方式：基于已确认业务口径与架构/功能设计。

## 1.3 修订记录

| 版本 | 日期 | 内容 |
|---|---|---|
| V0.1 | 2026-07-12 | 详细设计初版 |
| V0.3 | 2026-07-12 | 补齐源字段、指纹、建案条件、接口Schema与舍入契约 |
| V0.4 | 2026-07-12 | 与总设计独立评审修订保持一致 |
| V0.5 | 2026-07-12 | 补充非案件候选candidate_key和幂等检测键 |
| V0.6 | 2026-07-12 | 第三轮独立规格评审通过 |
| V0.7 | 2026-07-12 | 增加业务招待费未关联SAP时基于OA/合思直接判断及后续案件合并 |
| V0.8 | 2026-07-12 | 未关联业务单据双路径修订通过独立规格评审 |
| V0.9 | 2026-07-14 | 明确累计营业收入小于或等于0时本年累计所得税税负率取0 |
| V1.0 | 2026-07-16 | 增加递延所得税计提/转回准确性检查的数据与计算契约 |
| V1.1 | 2026-07-17 | 增加所得税退税进度监控及入账科目准确性检查契约 |

## 1.4 关键词

数据契约、公式、定点金额、风险指纹、所得税退税、证据关联、Agent输出schema、异常处理、验收案例。

## 1.5 摘要

本文定义六项核心监测能力（季度四项确定性监测、月度纳税调增科目准确性监测和所得税退税进度监控及入账科目准确性检查）的算法、数据结构、接口、正常/异常行为、去重、证据关联、金额精度、安全和测试要求，可直接作为实施计划和开发验收的输入。

## 1.6 缩略语清单

| 缩略语 | 英文全称 | 中文名 |
|---|---|---|
| API | Application Programming Interface | 应用程序接口 |
| RLS | Row-Level Security | 行级安全 |
| JSON | JavaScript Object Notation | 结构化数据格式 |
| P95 | 95th Percentile | 第95百分位 |
| DFX | Design for X | 面向质量属性的设计 |

## 1.7 简介

详细设计保持厂商和编程语言中立。所有公式和状态均是业务契约；具体技术栈可以替换，但不得改变符号、期间、阈值、主数据来源和人工责任边界。

# 2 上游文档引用

1. [总设计](../../superpowers/specs/2026-07-12-group-income-tax-risk-monitoring-platform-design.md)
2. [架构设计](../architecture/2026-07-12-group-income-tax-risk-monitoring-platform-architecture.md)
3. [功能设计](../function/2026-07-12-group-income-tax-risk-monitoring-platform-function.md)

# 3 实现设计：数据快照与质量门

## 3.1 实现概述

每个来源批次先落原始区并生成不可变清单，再按公司和期间构建标准快照。质量门只做确定性校验，不推测缺失值。通过的快照才能进入监测。

## 3.2 关键算法与流程

```text
对每个 source_batch：
    校验 schema_version、source_primary_key、period
    核对 record_count 与 control_total
    拒绝重复的 source_primary_key
    标准化 company_code、currency、amount_scale、sign

对每个 company_period：
    要求存在且仅存在一条有效 TaxMasterData 记录
    要求选定监测所需的全部来源数据集均已就绪
    若任一阻断性检查失败：
        生成 DataIssue；标记为 NOT_RUN
    否则：
        封存 AccountingSnapshot；生成 SnapshotReady
```

金额使用十进制定点类型；来源金额保留SAP公司账簿币种小数位，跨币种不在首期公式内混算。SAP信用方向、红字和冲销由数据字典转换为业务代数金额。

税率和历史平均税负率内部使用0至1的小数；上传值如带百分号，导入适配器标准化后保存。适用税率、历史税负率和5个百分点阈值的示例内部值分别为`0.25`、`0.09`和`0.05`。

精确计算契约：金额中间值使用`Decimal(38,12)`且不作中间舍入；税率/比率使用受控十进制输入、`Decimal(38,12)`计算。累计应纳税额和潜在累计应纳税额在`max`及乘税率后，按公司账簿币种小数位采用`ROUND_HALF_UP`舍入一次。系统累计递延所得税费用在“亏损 - 累计利润”乘递延所得税税率后按同一规则舍入，不取`max`。本季度应计提、计提差异、递延所得税本年应计提/转回额和潜在税务成本由已舍入税额与SAP账面金额相减，并按同一币种精度标准化后判断非0。税负率和偏离度使用未作展示舍入的`Decimal(38,12)`值与`0.05`比较，界面展示舍入不改变示警。

## 3.3 行为模型

### 3.3.1 正常流程

来源批次完成 → 控制总额相符 → 公司和期间匹配 → 主数据唯一有效 → 快照封存 → 发布可运行事件。

### 3.3.2 异常流程

- 公司不匹配：创建`COMPANY_UNMAPPED`。
- 主数据缺失/重复：创建`TAX_MASTER_MISSING/DUPLICATE`。
- 控制总额不平：创建`CONTROL_TOTAL_MISMATCH`。
- 数据未到齐：创建`SOURCE_NOT_READY`。
- 重复主键：创建`DUPLICATE_SOURCE_ROW`。

异常阻断对应公司本次依赖该快照的执行；不得产生部分结果、零值占位或“无风险”。

## 3.4 数据模型

### 3.4.1 数据结构定义

```text
TaxMasterData {
  company_code, company_name,
  effective_from, effective_to,
  tax_rate, deferred_tax_rate, loss_carryforward,
  average_tax_burden_rate_3y,
  source_file_name, file_checksum,
  upload_user, review_user, version
}

AccountingSnapshot {
  snapshot_id, company_code, period,
  source_batch_ids[], schema_versions[],
  record_counts{}, control_totals{},
  currency, amount_scale, rounding_mode,
  sealed_at
}

DataIssue {
  issue_id, company_code, period,
  monitor_type, code, severity,
  source_ref, details, owner, status
}
```

### 3.4.2 数据流转

原始记录只追加；标准化记录引用原始主键；快照引用来源批次；计算和Agent检测只引用快照编号，不直接读取不断变化的来源表。

## 3.5 接口设计

### 3.5.1 内部接口设计

适配器、质量服务和快照服务之间使用结构化批次契约；错误码区分可重试和不可重试。

### 3.5.2 内部接口定义

```text
IngestBatch(batch_id, source, extraction_time, period,
            mode, schema_version, payload_ref,
            source_primary_key_definition,
            currency, amount_scale,
            record_count, control_total,
            idempotency_key)
  -> {status, accepted_rows, rejected_rows,
      row_errors[], retryable, ingest_batch_id}
ValidateCompanyPeriod(company_code, period, monitor_types[]) -> ValidationReport
SealSnapshot(validation_report_id) -> snapshot_id
```

`mode`为FULL或INCREMENTAL；`status`为ACCEPTED、PARTIAL或REJECTED。每行数据必须携带按`source_primary_key_definition`构造的源主键。PARTIAL不得被视为数据就绪，除非对应监测的数据质量策略明确允许且所有拒绝行均与该监测无关。

## 3.6 代码实现要点

- 禁用二进制浮点金额。
- 数据库唯一约束保证来源主键和主数据有效期唯一。
- 快照封存后禁止修改，只能创建新版本。
- 日志不输出完整敏感摘要或附件内容。

# 4 实现设计：季度确定性监测

## 4.1 实现概述

季度计算服务从同一快照读取累计损益、SAP收到分红、当期所得税计提、SAP累计递延所得税费用、暂估、合思无票以及有效税务主数据，依次计算四个监测结果。递延所得税税率是独立公司主数据，不默认等同于适用税率。

## 4.2 关键算法与流程

```text
base_before_floor =
    cumulative_profit_total
  - sap_cumulative_dividend_received
  - cumulative_fair_value_change
  - loss_carryforward

cumulative_tax_payable =
    round_currency(max(base_before_floor, 0) * tax_rate,
                   ROUND_HALF_UP)

quarter_tax_should_accrue =
    cumulative_tax_payable
  - sap_prior_quarters_current_tax_provision

quarter_tax_difference =
    quarter_tax_should_accrue
  - sap_current_quarter_current_tax_provision

若 round_to_ledger_scale(quarter_tax_difference) != 0：
    生成 PROVISION_DIFFERENCE
```

`sap_cumulative_dividend_received`为SAP账上本年累计收到分红；不取向股东支付的分红。`sap_*_current_tax_provision`只包括当期所得税，不包括递延所得税和以前年度调整。本季度应计提可为负数。

```text
deferred_tax_base =
    loss_carryforward
  + cumulative_profit_total

system_cumulative_deferred_tax =
    round_currency(deferred_tax_base * deferred_tax_rate,
                   ROUND_HALF_UP)

current_year_deferred_tax_adjustment =
    round_currency(system_cumulative_deferred_tax
                   - sap_cumulative_deferred_tax_expense,
                   ROUND_HALF_UP)

若 current_year_deferred_tax_adjustment > 0：
    生成 DEFERRED_TAX_TO_ACCRUE，方向为 ACCRUE
若 current_year_deferred_tax_adjustment < 0：
    生成 DEFERRED_TAX_TO_REVERSE，方向为 REVERSE
若 current_year_deferred_tax_adjustment == 0：
    不示警
```

`loss_carryforward`沿用受控主数据中的非负余额语义，按`loss_carryforward - cumulative_profit`参与新公式，不做符号翻转；`deferred_tax_base`不取`max`或零下限。`sap_cumulative_deferred_tax_expense`按科目余额表 `1811030000` 的期末余额原值输入，不做符号翻转；查询成功但目标科目缺失时视为未计提并取0，接口失败、响应不合法或未导入时仍保持缺数据。历史`QUARTERLY_V2`继续按加法公式重放，新运行使用`QUARTERLY_V3`。

```text
若 cumulative_revenue <= 0：
    current_tax_burden = 0
否则：
    current_tax_burden = cumulative_tax_payable / cumulative_revenue
deviation = current_tax_burden - offline_average_tax_burden_rate_3y
若 abs(deviation) >= 0.05：
    deviation > 0 时生成 TAX_BURDEN_HIGH，否则生成 TAX_BURDEN_LOW
```

```text
potential_adjustment =
    sap_other_payables_accrual_balance
  + hesi_uninvoiced_reimbursement_amount

potential_tax_payable =
    round_currency(max(base_before_floor + potential_adjustment, 0) * tax_rate,
                   ROUND_HALF_UP)

potential_tax_cost =
    potential_tax_payable - cumulative_tax_payable

若 round_to_ledger_scale(potential_tax_cost) != 0：
    生成 POTENTIAL_TAX_COST
```

两项潜在调增来源均按风险正数口径进入公式。

## 4.3 行为模型

### 4.3.1 正常流程

读取快照和规则版本 → 校验字段 → 按规则版本计算三项或四项结果 → 保存代入值 → 创建检测记录 → 幂等创建/更新风险案件。历史`QUARTERLY_V1`保持原三项顺序重放；新增递延所得税的规则版本运行四项，避免历史结果漂移。

### 4.3.2 异常流程

- 税率、亏损或历史税负缺失/重复：对应监测不运行。
- 递延所得税税率或SAP累计递延所得税费用缺失：阻断该公司本次`QUARTERLY_V3`执行，不按0计算，也不保存其他监测的部分结果；历史`QUARTERLY_V2`按原公式重放。
- 营业收入≤0：税负率取0并继续计算偏离度，其他两项照常运行。
- SAP计提科目映射无效：阻断计提检查。
- 金额溢出、非有限值或币种不一致：阻断并告警。

## 4.4 数据模型

### 4.4.1 数据结构定义

```text
QuarterlyCalculationResult {
  company_code, quarter, snapshot_id,
  currency, amount_scale, rounding_mode,
  tax_master_version, rule_version,
  calculation_status,  // CALCULATED | NOT_CALCULABLE | FAILED
  alert_flag, alert_code, not_calculated_reason,
  cumulative_profit_total,
  sap_cumulative_dividend_received,
  cumulative_fair_value_change,
  loss_carryforward, tax_rate,
  deferred_tax_rate,
  sap_cumulative_deferred_tax_expense,
  deferred_tax_base,
  system_cumulative_deferred_tax,
  current_year_deferred_tax_adjustment,
  cumulative_tax_payable,
  prior_quarter_provision,
  current_quarter_should_accrue,
  current_quarter_actual_provision,
  provision_difference,
  cumulative_revenue,
  current_tax_burden,
  historical_tax_burden,
  tax_burden_deviation,
  accrual_balance,
  uninvoiced_reimbursement,
  potential_adjustment,
  potential_tax_payable,
  potential_tax_cost
}
```

字段可空规则：`calculation_status=CALCULATED`时对应监测的所有必需输入和结果非空；`NOT_CALCULABLE`时不可计算结果必须为空且`not_calculated_reason`非空；禁止用0代替未计算。单个`QuarterlyCalculationResult`可按监测类型拆为四条结果，以便当期所得税计提、递延所得税计提/转回、税负和潜在成本分别表达状态。

### 4.4.2 数据流转

计算结果、风险检测记录和公式代入明细一并事务写入；案件状态更新失败时可根据检测记录重试，不重复计算来源数据。

## 4.5 接口设计

### 4.5.1 内部接口设计

季度计算为纯函数式服务：输入快照引用、主数据和规则版本，输出结构化结果，不直接改变风险状态。

### 4.5.2 内部接口定义

```text
CalculateQuarterly(company_code, quarter, snapshot_id,
                   tax_master_version, rule_version)
  -> QuarterlyCalculationResult | DataIssue[]
```

## 4.6 代码实现要点

- `max`只用于累计计税基础；不得对本季度应计提再次取`max`。
- 递延所得税基础严格使用`loss_carryforward - cumulative_profit_total`，不得取`max`或隐式翻转输入符号。
- 递延所得税调整额按账簿精度非0示警；正数计提、负数转回，SAP费用/计提正数、收益/转回负数。
- `abs(deviation) >= 0.05`中的0.05表示5个百分点。
- 最终税额只在规定时点按`ROUND_HALF_UP`舍入；任何展示舍入均不得反向改变计算值或示警。
- 保存每个字段的来源表、源字段和源记录引用。

# 5 实现设计：月度语义监测

## 5.1 实现概述

规则服务确定公司范围，候选生成器对本年1月至本月明细宽筛，证据服务尝试建立跨SAP、合思、OA关联，专业Agent深判并输出严格结构化建议。业务招待费先构建“精确关联SAP的业务链”和“未关联SAP的OA/合思业务单据”两类判断对象；无法关联任何前置单据的SAP凭证只进入覆盖清单，不借用同公司其他业务单据作证据。

## 5.2 关键算法与流程

```text
scope = select_company_scope(monitor_type, month)

若 monitor_type == BUSINESS_ENTERTAINMENT：
    sap_items = load_ytd_sap_items(scope, month)
    business_docs = load_ytd_oa_hesi_business_documents(scope, month)
    exact_links = build_exact_links(sap_items, business_docs)
    items = build_sap_linked_packs(exact_links)
    items += build_unlinked_business_document_packs(
                 business_docs 中不属于 exact_links 的记录,
                 canonical_priority = HESI_REIMBURSEMENT_THEN_OA_APPLICATION)
    save_unlinked_sap_coverage_list(sap_items 中不属于 exact_links 的记录)
否则：
    items = load_ytd_sap_items(scope, month)

对每个 item：
    candidate = keyword_or_semantic_recall_filter(item)
    若不存在 candidate：
        继续处理下一项
    evidence_pack = link_evidence(item)
    source_mode = determine_source_mode(item, evidence_pack)
    result = domain_agent.classify(source_mode, evidence_pack)
    validate_output_schema(result)
    evidence_reviewer.verify(result, evidence_pack)
    save_detection(result)
    若 result.semantic_label 属于 SUSPECTED_MISPOSTING_LABELS：
        若 source_mode == SAP_LINKED：
            upsert_sap_risk_case(result)
        否则，若 monitor_type == BUSINESS_ENTERTAINMENT
                且 item.is_oa_or_hesi_business_document：
            upsert_business_document_risk_case(result)  // 未关联
        否则：
            create_or_update_evidence_task(result)
    否则，若 result.semantic_label == INSUFFICIENT_EVIDENCE：
        create_or_update_evidence_task(result)
    // CURRENT_ACCOUNT_REASONABLE 只保留检测记录
```

公司范围：

```text
招待费：业务招待费公司清单
福利费：累计福利费 - 累计工资薪金 * 14% > 0
公益捐赠：累计公益性捐赠 - 累计利润总额 * 12% > 0
```

标准分类标签至少包括：`CURRENT_ACCOUNT_REASONABLE`、`INTERNAL_MEAL_OR_WELFARE`、`CONFERENCE_EXPENSE`、`EMPLOYEE_EDUCATION`、`BUSINESS_ENTERTAINMENT`、`ADVERTISING_PROMOTION`、`SPONSORSHIP`、`INSUFFICIENT_EVIDENCE`。

关联优先级：1）合思/OA直接携带SAP凭证号和行项目；2）SAP分配号/参考字段精确包含报销单号或申请单号；以上属于EXACT。仅以公司、金额、日期窗口、人员、部门或收款方相似匹配属于FUZZY，不自动归并。

业务招待费主记录有两种模式：`SAP_LINKED`表示SAP凭证行已精确关联OA/合思；`BUSINESS_DOCUMENT_UNLINKED`表示OA申请或合思报销未关联SAP，但可根据申请事由、报销事由、接待对象、参与人员、场景和金额等自身字段直接判断。OA与合思之间如有精确流程关联，使用合思报销单为主记录、OA申请为证据；否则分别作为主记录。第二种模式的疑似错入可以创建正式风险，标记“SAP凭证待定位”，不得将该业务单据作为同公司任意SAP凭证的证据。没有精确业务单据关联的SAP凭证只记录在覆盖清单，避免错误归因。

## 5.3 行为模型

### 5.3.1 正常流程

范围筛选 → 本年累计明细 → 宽筛 → 证据关联 → 专业Agent深判 → 证据复核 → 案件更新 → 人工处理。

### 5.3.2 异常流程

- 关键关联号缺失：保留为独立SAP或OA/合思主记录；模糊关联只作候选提示，不自动归并。
- 证据冲突：输出`INSUFFICIENT_EVIDENCE`并列出冲突。
- Agent非结构化输出：拒绝入库并自动重试一次；仍失败进入人工队列。
- 模型超时/限流：有限重试，超过上限保留候选并标记未深判。
- 越权或工具白名单拒绝：停止该请求并产生安全审计事件。

## 5.4 数据模型

### 5.4.1 数据结构定义

```text
EvidenceRef {
  source_system, source_type, source_id,
  company_code, cited_fields[], cited_text_spans[],
  relation_type, relation_quality  // EXACT | FUZZY | MISSING
}

SemanticDetection {
  detection_id, candidate_key,
  case_key?,  // 仅正式风险存在
  company_code, period, monitor_type,
  canonical_record_type,  // SAP_VOUCHER_LINE | OA_APPLICATION | HESI_REIMBURSEMENT
  source_system, source_document_id, source_line_id,
  sap_link_status,  // LINKED | UNLINKED
  sap_fiscal_year?, voucher_no?, line_item_no?,
  current_account?, posting_date?, amount,
  risk_amount_source,
  semantic_label,
  recommended_accounts[],
  rationale_summary,
  evidence_refs[], missing_evidence[],
  confidence_tier,  // HIGH | MEDIUM | LOW
  rule_version, model_version,
  prompt_version, case_library_version,
  snapshot_id, created_at
}

EvidenceTask {
  task_id, candidate_key,
  company_code, period, monitor_type,
  candidate_source_refs[], missing_fields[],
  relation_quality, reason,
  status, assignee, created_at, updated_at
}

SapLinkCoverageItem {
  company_code, period, sap_fiscal_year,
  voucher_no, line_item_no, amount,
  link_status,  // LINKED | UNLINKED
  evaluated_via_business_document,  // 未关联SAP恒为false
  snapshot_id, created_at
}
```

`rationale_summary`只保存简洁业务解释，不保存模型内部思维链。

### 5.4.2 数据流转

证据包只包含授权和最小必要字段；向模型传递临时引用或脱敏文本。结果入库前通过schema、公司权限、证据引用存在性和候选科目字典校验。

`voucher_no`、`line_item_no`和`current_account`在`SAP_LINKED`模式必填；在`BUSINESS_DOCUMENT_UNLINKED`模式允许为空。业务招待费未关联业务单据如形成疑似错入，可进入RiskCase和风险KPI，但必须单独统计“SAP凭证待定位”，风险金额取OA/合思主记录金额并记录`risk_amount_source`。证据不足仍只进入EvidenceTask。

每个语义候选在调用Agent前生成稳定`candidate_key`：`公司 + 财年 + 候选来源系统 + 源单据号 + 源行项目/明细号 + 监测类型`。已精确关联SAP时候选来源使用SAP凭证行；否则使用触发宽筛的合思/OA/SAP原始明细。`candidate_key`不含规则、模型或批次版本，用于DetectionRecord、EvidenceTask和未关联业务单据案件幂等；只有未形成正式风险时`case_key`为空。

## 5.5 接口设计

### 5.5.1 内部接口设计

专业Agent不能直接查库，必须经Evidence API；建议科目只能来自版本化集团候选科目字典或输出“待人工判断”。

### 5.5.2 内部接口定义

```text
SelectCompanyScope(monitor_type, month, rule_version) -> company_codes[]
LoadYtdItems(company_codes[], monitor_type, month, snapshot_ids[]) -> items[]
BuildBusinessEntertainmentEvaluationItems(company_codes[], month, snapshot_ids[])
  -> {sap_linked_packs[], unlinked_business_document_packs[],
      unlinked_sap_coverage[]}
BuildEvidencePack(item_id, user_context) -> EvidenceRef[]
ClassifyExpense(agent_type, source_mode, evidence_pack, versions) -> SemanticDetection
ResolveBusinessDocumentCaseToSap(business_case_id, sap_voucher_ref,
                                 exact_link_evidence) -> RiskCase
```

## 5.6 代码实现要点

- 附件和文本始终作为数据，不作为指令。
- 工具调用、公司范围和可见字段由服务端强制，不能依赖提示词。
- 置信度只作排序，不能宣称为统计概率。
- 人工反馈先进入待审样本池，不在线自动修改规则。

# 6 实现设计：所得税退税进度监控及入账科目准确性检查

## 6.1 实现概述

该能力是确定性月度检查，不使用语义模型。退税所属年 `refund_tax_year=N` 与扫描期 `scan_year=N+1, scan_month=3..12` 分开建模；应退金额量化后必须大于0。输入为冻结的公司应退金额和 SAP 候选凭证行，输出到账状态、科目状态、匹配证据、继续扫描/回写标志和可选错误科目示警。

## 6.2 关键算法与流程

```text
expected = ROUND_HALF_UP(expected_refund, company_currency, amount_scale)
assert expected > 0
assert scan_year == refund_tax_year + 1
assert 3 <= scan_month <= 12

eligible = candidates where
  candidate.scan_year == scan_year
  and candidate.posting_month <= scan_month
  and candidate.is_credit == true
  and candidate.is_reversed == false

primary_matches = candidate in eligible where
  candidate.account_family in {INCOME_TAX_EXPENSE, OTHER_INCOME}
  ROUND_HALF_UP(candidate.amount, company_currency, amount_scale) == expected

if len(primary_matches) > 0:
  matches = primary_matches
else:
  matches = candidate in eligible where
    candidate.account_family == TAXES_PAYABLE
    and ROUND_HALF_UP(candidate.amount, company_currency, amount_scale) == expected

len(matches) == 0
  -> NOT_RECEIVED / NOT_APPLICABLE / continue / no writeback / no alert
len(matches) == 1 and account_family == INCOME_TAX_EXPENSE
  -> RECEIVED / CORRECT / stop / writeback / no alert
len(matches) == 1 and account_family == OTHER_INCOME
  -> RECEIVED / WRONG_ACCOUNT / stop / writeback
     / alert REFUND_BOOKED_TO_WRONG_ACCOUNT
len(matches) == 1 and account_family == TAXES_PAYABLE
  -> RECEIVED / WRONG_ACCOUNT / stop / writeback
     / alert REFUND_BOOKED_TO_WRONG_ACCOUNT
len(matches) > 1
  -> AMBIGUOUS / AMBIGUOUS / continue / no writeback / no wrong-account case
```

“完全等额”只比较量化后的单条明细，不使用容差、不把多条求和、不按科目名称模糊分类。第一阶段存在候选时不再考虑第二阶段；选中阶段内多个候选保持歧义。唯一候选必须保留来源行 ID、科目代码/名称、凭证号、行项目和过账日。

## 6.3 行为模型

### 6.3.1 正常流程

3-12月的幂等运行先导入飞书状态，再读取本地仍为 `PENDING` 的公司年度退税目标，冻结 SAP 行后逐公司计算。飞书已手工标记“已退税”的目标导入即置为 `RECEIVED`，不生成扫描或回写任务；SAP唯一正确或错误科目命中也把本地目标置为 `RECEIVED`，并在同一数据库事务中新增 outbox。下月均据本地年度状态跳过。outbox 在事务外按公司代码唯一定位飞书多维表记录并写入“已退税”，失败仅重试投递。

### 6.3.2 异常流程

- 1-2月、扫描年不是 N+1、应退金额量化后不大于0：拒绝运行或阻断公司。
- 币种/scale 不一致、候选缺少借贷/冲销/行身份：数据质量失败，不猜测或补默认值。
- 多条等额候选：保存 `AMBIGUOUS`，继续次月扫描，不自动回写或建科目错误案。
- 飞书不可用：本地检测和 `RECEIVED` 主事实不回滚，outbox 保持待重试。
- SAP 或飞书源对象合同不满足启用门禁：能力保持默认关闭。

## 6.4 数据模型

### 6.4.1 数据结构定义

```text
IncomeTaxRefundTarget {
  target_id, company_id, refund_tax_year,
  expected_refund_amount, currency, amount_scale,
  source_file_ref, source_row_key,
  status: PENDING | RECEIVED,
  latest_detection_id?, received_at?, row_version
}

IncomeTaxRefundCandidate {
  source_line_id, account_family,
  account_code, account_name,
  document_number, line_item, posting_date,
  amount, currency, amount_scale,
  is_credit, is_reversed
}

IncomeTaxRefundDetection {
  detection_id, run_id, target_id,
  refund_tax_year, scan_year, scan_month,
  receipt_status: NOT_RECEIVED | RECEIVED | AMBIGUOUS,
  booking_status: NOT_APPLICABLE | CORRECT | WRONG_ACCOUNT | AMBIGUOUS,
  expected_amount, matched_candidate_refs[],
  continue_scanning, requires_writeback,
  alert_code?, rule_version, snapshot_id, created_at
}

RefundWritebackOutbox {
  outbox_id, target_id, desired_value,
  idempotency_key, status, attempt_count,
  last_error_code?, delivered_at?
}
```

目标唯一键至少包含公司和退税所属年；检测键还包含扫描年月、冻结快照和规则版本。飞书多维表“是否已收到退税”没有年度维度，因此只能作为展示同步字段，不能作为跨年度跳过扫描的唯一依据。

### 6.4.2 数据流转

受控来源形成退税目标版本；SAP 凭证行形成不可变候选快照；领域结果先原子写入检测、案件/本地目标和 outbox，再由专用投递器更新外部 Base。所有阶段保存原值和量化值。

## 6.5 接口设计

### 6.5.1 内部接口设计

运行入口只接受3-12月并按参数生成稳定幂等键。由于未提供具体每月运行日，不内置日历时间，由幂等 API 或外部调度调用。写回配置固定 Base token、数据表 ID、公司代码字段 ID 和状态字段 ID，应用凭据只注入专用 Worker。

### 6.5.2 内部接口定义

```text
StartRefundRun(refund_tax_year, scan_year, scan_month, source_versions)
  -> run_id
EvaluateIncomeTaxRefund(target, candidates, rule_version)
  -> IncomeTaxRefundDetection
ListRefundResults(refund_tax_year, scan_month, company_scope)
  -> received_companies[], not_received_companies[],
     refund_amounts[], booking_accounts[]
DeliverRefundWriteback(outbox_id) -> delivery_status
```

SAP利润表只提供报表项目累计值，科目余额表只提供余额，汇总科目发生额只提供科目借贷汇总。“汇算清缴相关科目明细”已提供凭证号、科目、摘要、KSL和借贷标志；所得税费用取 `6801010000/6801020000/6801030000`，其他收益取 `6112010000/6112020000/6112040000`，应交税费只取 `2221130000`（应交税费-企业所得税）。三类候选均对每条明细分别按 `-KSL` 计算并保留来源字段，不汇总且不从名称推断；其他`2221*`科目明确排除。当前响应仍缺行项目唯一标识、过账日期和冲销标志，因此在取得完整 SAP 凭证行合同前只能完成逐条金额映射，不能接线退税单条等额匹配。

## 6.6 代码实现要点

- 金额始终以显式 `Money(currency, scale)` 量化，舍入模式固定为 `ROUND_HALF_UP`。
- 当前 AJ 原始公式值存在4位小数而单元格按2位显示；暂定 `scale=2` 必须经业务验收。
- 飞书多维表按公司代码精确匹配；无匹配或重复匹配均不得写入，远端状态已相等时直接幂等成功。
- 本地 `RECEIVED` 与 outbox 同事务提交；外部 API 不得加入数据库事务。
- 错误科目在唯一其他收益命中或第一阶段零命中后唯一应交税费命中时建案，示警代码固定为 `REFUND_BOOKED_TO_WRONG_ACCOUNT`。

# 7 实现设计：风险案件与工作流

## 7.1 实现概述

案件服务把不同批次和版本对同一事项的检测归并为稳定案件，并保存完整检测历史。工作流服务强制状态转换、角色权限和审计。

## 7.2 关键算法与流程

```text
numeric_case_key = hash(company_code,
                        fiscal_year,
                        quarter,
                        monitor_type)

refund_case_key = hash(company_code,
                       refund_tax_year,
                       INCOME_TAX_REFUND_ACCOUNT_ACCURACY)

semantic_candidate_key = hash(company_code,
                              fiscal_year,
                              candidate_source_system,
                              candidate_source_id,
                              candidate_source_line_id,
                              monitor_type)

semantic_case_key = hash(company_code,
                         sap_fiscal_year,
                         sap_voucher_no,
                         sap_line_item_no,
                         monitor_type)

business_document_case_key = hash(company_code,
                                  fiscal_year,
                                  source_system,
                                  source_document_id,
                                  source_line_id,
                                  monitor_type)

detection_subject_key = numeric_case_key
                        或 semantic_candidate_key

detection_key = hash(detection_subject_key, batch_id,
                     rule_version, model_version,
                     prompt_version, snapshot_id)

按 case_key 新增或更新 RiskCase
按 detection_key 插入 DetectionRecord
附加全部精确/模糊证据引用
```

季度数值类以公司/财年/季度/监测类型区分案件；退税错误科目以公司/退税所属年/监测类型区分案件；已关联业务招待费及福利费/捐赠以SAP凭证行为主记录；未关联业务招待费以OA/合思业务单据为主记录。案件指纹不含规则/模型版本；检测键始终基于数值主体键或语义候选键，并包含版本。FUZZY关联不生成`semantic_case_key`，但未关联业务单据如形成疑似错入使用`business_document_case_key`建正式风险。后续建立EXACT关联时，事务性合并业务单据案件到SAP案件并设置`merged_into_case_id`，不重复累计风险数量和金额。

## 7.3 行为模型

### 7.3.1 正常流程

`新发现→待分派→待公司确认`；确认风险进入待改账，登记更正凭证后集团复核关闭；入账合理需理由和依据；信息不足补材料后重新判断。未关联业务招待费确认风险后先进入`待定位SAP凭证`，建立精确关联并合并案件后再进入待改账。

### 7.3.2 异常流程

- 非法状态跳转：返回`INVALID_TRANSITION`，不改变案件。
- 非授权公司：返回`FORBIDDEN`并记录安全事件。
- 更正凭证号重复或不存在：保持待改账并提示校验失败。
- 并发更新：使用版本号乐观锁，冲突方刷新后重试。

## 7.4 数据模型

### 7.4.1 数据结构定义

```text
RiskCase {
  case_id, case_key, company_code,
  monitor_type, canonical_record_type,
  canonical_source_ref, sap_link_status,
  risk_amount, risk_amount_source, risk_direction,
  priority, status, assignee,
  merged_into_case_id?,
  latest_detection_id, created_at, updated_at,
  row_version
}

ReviewAction {
  action_id, case_id, actor, actor_role,
  from_status, action, to_status,
  reason, attachment_refs[],
  correction_voucher_no, created_at
}
```

### 7.4.2 数据流转

检测写入更新案件最新摘要；人工操作追加ReviewAction事件并更新案件状态；审计记录不可覆盖。

## 7.5 接口设计

### 7.5.1 内部接口设计

所有案件读写携带用户组织范围；导出服务重复执行权限过滤，不信任前端筛选条件。

### 7.5.2 内部接口定义

```text
CreateOrUpdateRisk(detection) -> RiskCase
ResolveBusinessDocumentCaseToSap(business_case_id, sap_voucher_ref,
                                 exact_link_evidence) -> RiskCase
TransitionRisk(case_id, expected_row_version, action, evidence) -> RiskCase
ExportRisks(user_context, filters) -> export_job_id
```

## 7.6 代码实现要点

- 状态机集中实现，不在UI分散判断。
- 风险与数据异常使用不同实体和统计口径。
- 所有高敏操作写前后值、操作者、条件和时间。

# 8 DFX分析

## 8.1 可靠性分析

- 任务按公司隔离，有限退避重试，超过上限进入异常队列。
- 相同快照/版本确定性重跑一致，案件创建幂等。
- 应用、规则和模型/提示词均有版本、审批、回放和回滚。
- 数据库、对象存储和审计日志按集团策略备份并定期恢复演练。

## 8.2 异常处理设计

错误分为数据质量、业务不可计算、可重试技术错误、不可重试技术错误和安全拒绝。每类均有稳定错误码、责任人、是否阻断、是否可重试及用户可读说明。

| 类别 | 示例 | 处理 |
|---|---|---|
| 数据质量 | 主数据缺失、控制总额不平 | 阻断对应公司/监测，待补数 |
| 可重试技术错误 | 超时、限流 | 退避重试后异常队列 |
| 不可重试技术错误 | schema不兼容 | 立即失败并通知维护人 |
| 安全拒绝 | 越权、非法工具调用 | 拒绝、审计和告警 |

## 8.3 性能分析

- 按公司并行、单公司内按依赖顺序运行。
- 宽筛批量执行，深判只处理候选；证据批量加载避免逐条跨系统查询。
- 集团看板使用聚合数据，凭证明细按需分页加载。
- 验收以100+公司批次成功率≥98%和数据就绪后T+2完成月度清单为底线。

## 8.4 安全和韧性分析

- RBAC和数据库/语义索引RLS双层授权。
- 企业受控模型端点，业务数据不用于公共训练。
- 个人字段最小化和脱敏；日志避免记录敏感全文。
- OA、合思文本和附件防提示注入；Agent只用白名单工具。
- 接口密钥进密钥管理系统，传输和静态数据加密。

# 9 验收测试矩阵

## 9.1 数值测试

| 场景 | 输入 | 预期 |
|---|---|---|
| 标准计提 | 利润1000、收到分红100、公允价值收益50、亏损200、税率25% | 累计应纳税额162.5万元 |
| 本季差异 | 以前季度计提90、本季实际70 | 本季应提72.5，差异+2.5，少计提 |
| 递延所得税应转回 | 亏损200、累计利润1000、递延税率25%、SAP累计计提280 | 系统累计-200，本年应转回-480，示警REVERSE |
| 递延所得税应转回 | 亏损200、累计利润1000、递延税率25%、SAP累计计提320 | 系统累计300，本年应计提/转回-20，示警REVERSE |
| 递延所得税零差异 | 亏损200、累计利润1000、递延税率25%、SAP累计计提300 | 本年调整0，不示警 |
| 税负偏低 | 收入5000、历史9% | 本年3.25%，偏离-5.75个百分点，示警 |
| 阈值边界 | 偏离±5个百分点 | 均示警 |
| 潜在成本 | 暂估140、合思无票30 | 调增170，潜在税额205，成本42.5 |
| 累计税额下降 | 本季应提为负 | 保留负数，提示需冲回 |
| 收入非正 | 收入≤0 | 不执行除法，税负率取0并继续判断与历史平均税负率的偏离 |

## 9.2 语义测试

| 当前科目与文本 | 预期候选 |
|---|---|
| 招待费“内部培训午餐” | 职工教育经费/福利费，引用OA证据 |
| 未关联SAP的OA申请“部门内部培训午餐” | 建业务单据风险，标记SAP凭证待定位，不关联任意SAP凭证 |
| OA申请与合思报销精确关联但均未关联SAP | 以合思报销为主、OA为证据，只生成一个业务单据风险 |
| SAP凭证无法关联任何前置单据 | 进入SAP未关联覆盖清单，不借用同公司其他OA/合思单据 |
| 上述OA案件后续找到SAP凭证 | 合并到SAP案件，风险数量和金额不重复 |
| 福利费“客户商务宴请” | 业务招待费 |
| 捐赠“冠名并获得品牌露出” | 赞助或广告宣传；不作最终税务定性 |

黄金样本由财务、税务双人标注并仲裁；对未命中明细抽样估算漏报。试点召回≥90%，正式上线≥95%，高置信度准确率≥80%，已知典型案例不得漏检。

## 9.3 所得税退税测试

| 场景 | 输入 | 预期 |
|---|---|---|
| 未到账 | 无贷方未冲销等额行 | `NOT_RECEIVED`，不回写、无风险、次月继续 |
| 正确入账 | 唯一等额所得税费用贷方未冲销行 | `RECEIVED + CORRECT`，本地停止、创建回写事件 |
| 错误入账 | 唯一等额其他收益贷方未冲销行 | `RECEIVED + WRONG_ACCOUNT`，示警`REFUND_BOOKED_TO_WRONG_ACCOUNT` |
| 应交税费兜底 | 第一阶段零命中，唯一等额应交税费贷方未冲销行 | `RECEIVED + WRONG_ACCOUNT`，网页标记“已退税但入账至应交税费” |
| 第一阶段优先 | 其他收益与应交税费均唯一等额 | 只采用其他收益候选，网页标记“已退税但入账至其他收益” |
| 多候选 | 两条贷方未冲销等额行 | `AMBIGUOUS`，不回写、不建科目错误案、次月继续 |
| 舍入边界 | 应退31.4375、SAP 31.44、scale=2 | `ROUND_HALF_UP`后完全等额 |
| 非容差 | 应退10.004、SAP 10.005、scale=2 | 分别量化为10.00和10.01，不匹配 |
| 非候选行 | 借方或已冲销等额行 | 排除，不确认到账 |
| 期间错误 | 扫描月为1/2月或扫描年不等于N+1 | 拒绝运行 |
| 写回失败 | 本地已`RECEIVED`、远端暂不可用 | 本地状态不回滚，outbox重试 |
| 飞书手工已退税 | 来源状态为“已退税” | 本地直接`RECEIVED`，不扫描、不新建回写任务 |

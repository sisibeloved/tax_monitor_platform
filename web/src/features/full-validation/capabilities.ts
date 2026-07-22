export type CapabilityCode =
  | "current_tax_accrual"
  | "deferred_tax"
  | "tax_burden"
  | "potential_tax_cost"
  | "tax_adjustment_account_accuracy"
  | "refund";

export type CapabilityStage = "LIVE" | "IN_PROGRESS";
export type ReadinessStatus = "READY" | "PARTIAL" | "MISSING";

export interface CapabilityReadinessItem {
  item: string;
  status: ReadinessStatus;
  detail: string;
}

export interface CapabilityDefinition {
  code: CapabilityCode;
  name: string;
  shortName: string;
  frequency: string;
  stage: CapabilityStage;
  description: string;
  output: string;
  unavailableReason?: string;
  readiness?: CapabilityReadinessItem[];
}

export const CAPABILITIES: readonly CapabilityDefinition[] = [
  {
    code: "current_tax_accrual",
    name: "季度应计提所得税准确性检查",
    shortName: "季度所得税",
    frequency: "每季度",
    stage: "LIVE",
    description: "核对系统测算应计提额与 SAP 实际计提额。",
    output: "差异公司、应计提额、实际计提额、计提差异",
  },
  {
    code: "deferred_tax",
    name: "递延所得税计提/转回准确性检查",
    shortName: "递延所得税",
    frequency: "每季度",
    stage: "LIVE",
    description: "核对系统测算的递延所得税计提或转回与账面金额。",
    output: "应计提或转回公司、测算额、SAP 累计计提额",
  },
  {
    code: "tax_burden",
    name: "当年累计税负率异常监测",
    shortName: "累计税负率",
    frequency: "每季度",
    stage: "LIVE",
    description: "识别本年累计税负率与三年平均税负率的显著偏离。",
    output: "异常公司、本年税负率、历史税负率、偏离度",
  },
  {
    code: "potential_tax_cost",
    name: "潜在纳税调增税务成本",
    shortName: "潜在调增成本",
    frequency: "每季度",
    stage: "LIVE",
    description: "量化暂估及无票报销可能形成的所得税成本。",
    output: "潜在调增金额、潜在应纳税额、潜在税务成本",
  },
  {
    code: "tax_adjustment_account_accuracy",
    name: "纳税调增科目准确性检查",
    shortName: "调增科目准确性",
    frequency: "每月",
    stage: "IN_PROGRESS",
    description: "识别业务招待费、福利费及公益性捐赠疑似错入明细。",
    output: "疑似错入明细、证据、置信度、改账建议、复核任务",
    unavailableReason:
      "语义规则和离线流程已具备，但真实 SAP、合思与 OA 明细尚未形成统一全量批次，当前不输出集团级风险数量。",
    readiness: [
      {
        item: "业务招待费语义规则",
        status: "READY",
        detail: "判断标签、证据引用和改账建议流程已完成离线验证。",
      },
      {
        item: "福利费及捐赠规则",
        status: "READY",
        detail: "比例门禁和专业语义策略已实现。",
      },
      {
        item: "合思报销明细",
        status: "PARTIAL",
        detail: "接口已配置，待完成月度全量字段映射及公司范围验收。",
      },
      {
        item: "SAP 与 OA 证据链",
        status: "MISSING",
        detail: "待接入真实凭证行及 OA 申请、自采报销和物料领用明细。",
      },
      {
        item: "集团级真实批次",
        status: "MISSING",
        detail: "待完成真实数据端到端运行、抽样复核和误报率验收。",
      },
    ],
  },
  {
    code: "refund",
    name: "所得税退税进度监控及入账科目准确性检查",
    shortName: "所得税退税",
    frequency: "每年 3-12 月",
    stage: "LIVE",
    description: "跟踪退税到账进度并检查入账科目。",
    output: "已退税、未退税公司，退税金额及入账科目",
  },
] as const;

export const LIVE_CAPABILITY_CODES = CAPABILITIES.filter(
  (capability) => capability.stage === "LIVE",
).map((capability) => capability.code);

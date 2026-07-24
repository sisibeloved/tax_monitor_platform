import type { CapabilityCode, CapabilityDefinition } from "./capabilities";
import type {
  FullValidationReport,
  SourceStatus,
  ValidationCompany,
  ValidationStatus,
} from "./types";

export const VALUE_LABELS: Record<string, string> = {
  cumulative_profit: "损益表累计利润总额",
  received_dividends: "累计分红金额",
  fair_value_change: "累计公允价值变动损益",
  loss_carryforward: "可弥补以前年度亏损",
  tax_rate: "所得税税率",
  prior_quarter_current_tax: "以前季度SAP所得税计提金额",
  current_quarter_should_accrue: "本季度应计提",
  current_quarter_current_tax: "SAP本季度计提",
  difference: "计提差异",
  deferred_tax_rate: "递延所得税税率",
  deferred_tax_base: "递延所得税计税基础",
  system_cumulative_deferred_tax: "系统累计递延所得税",
  sap_cumulative_deferred_tax_expense: "SAP累计已计提",
  adjustment: "应计提/转回",
  refund_amount: "应退税金额",
  match_count: "等额候选数",
  booking_account: "入账科目",
  booking_account_family: "入账科目类别",
  match_stage: "匹配阶段",
  receipt_source: "到账认定来源",
  cumulative_revenue: "损益表累计营业收入",
  current_tax_burden: "本年累计税负率",
  historical_tax_burden: "3年平均税负率",
  deviation: "偏离度",
  other_payables_accrual: "其他应付款暂估余额",
  reimbursement_expense_total: "合思报销费用金额",
  invoice_approved_total: "发票核准金额",
  hesi_no_invoice: "合思无票报销金额",
  potential_adjustment: "潜在纳税调增金额",
  cumulative_tax_payable: "原累计应纳税额",
  potential_tax_payable: "潜在应纳税额",
  potential_tax_cost: "潜在税务成本",
  welfare_cumulative: "福利费累计金额",
  salary_cumulative: "工资薪金累计金额",
  welfare_deduction_limit: "福利费税前扣除限额",
  welfare_adjustment: "福利费纳税调增额",
  welfare_detail_selected: "是否进入福利费明细检查",
  welfare_abnormal_candidate_count: "福利费疑似错入候选数",
  welfare_alert_count: "福利费示警明细数",
  welfare_alert_amount: "福利费示警金额",
  donation_cumulative: "公益性捐赠累计金额",
  donation_abnormal_candidate_count: "捐赠疑似错入候选数",
  donation_alert_count: "捐赠示警明细数",
  business_entertainment_cumulative: "业务招待费累计金额",
  business_entertainment_detail_count: "业务招待费明细数",
  business_entertainment_alert_count: "业务招待费示警明细数",
  business_entertainment_alert_amount: "业务招待费示警金额",
  business_entertainment_hesi_detail_count: "合思报销明细取证数",
  business_entertainment_hesi_invoice_count: "合思发票取证数",
  business_entertainment_hesi_application_count: "业务招待申请单取证数",
  business_entertainment_evidence_status: "业务招待费证据链状态",
};

export const VALUE_ORDER: Partial<Record<CapabilityCode, string[]>> = {
  current_tax_accrual: [
    "tax_rate",
    "current_quarter_should_accrue",
    "current_quarter_current_tax",
    "difference",
  ],
  deferred_tax: [
    "loss_carryforward",
    "cumulative_profit",
    "deferred_tax_base",
    "deferred_tax_rate",
    "system_cumulative_deferred_tax",
    "sap_cumulative_deferred_tax_expense",
    "adjustment",
  ],
  refund: [
    "refund_amount",
    "match_count",
    "match_stage",
    "booking_account_family",
    "booking_account",
    "receipt_source",
  ],
  tax_burden: ["current_tax_burden", "historical_tax_burden", "deviation"],
  potential_tax_cost: [
    "other_payables_accrual",
    "hesi_no_invoice",
    "potential_adjustment",
    "potential_tax_payable",
    "potential_tax_cost",
  ],
  tax_adjustment_account_accuracy: [
    "business_entertainment_cumulative",
    "business_entertainment_detail_count",
    "business_entertainment_alert_count",
    "business_entertainment_alert_amount",
    "business_entertainment_hesi_detail_count",
    "business_entertainment_hesi_invoice_count",
    "business_entertainment_hesi_application_count",
    "business_entertainment_evidence_status",
    "welfare_cumulative",
    "salary_cumulative",
    "welfare_deduction_limit",
    "welfare_adjustment",
    "welfare_detail_selected",
    "welfare_abnormal_candidate_count",
    "welfare_alert_count",
    "welfare_alert_amount",
    "donation_cumulative",
    "donation_abnormal_candidate_count",
    "donation_alert_count",
  ],
};

const STATUS_LABELS: Record<ValidationStatus, string> = {
  ALERT: "示警",
  CLEAR: "正常",
  BLOCKED: "阻断",
  NOT_APPLICABLE: "不适用",
};

const FIXED_HEADERS = [
  "年度",
  "季度",
  "截至期间",
  "数据生成时间",
  "监测功能",
  "公司代码",
  "公司名称",
  "结果状态",
  "检查结论",
  "告警代码",
  "阻断/限制原因",
  "证据受限",
  "主数据问题",
  "取数错误",
  "适配错误",
  "数据源状态",
  "等额候选明细",
] as const;

export function buildValidationDetailsCsv(
  report: FullValidationReport,
  capability: Pick<CapabilityDefinition, "code" | "name">,
  companies: readonly ValidationCompany[],
): string {
  const valueKeys = collectValueKeys(capability.code, companies);
  const headers = [
    ...FIXED_HEADERS,
    ...valueKeys.map((key) => `${VALUE_LABELS[key] ?? key}（${key}）`),
  ];
  const records = companies.map((company) => {
    const result = company.monitor_results[capability.code];
    if (result === undefined) {
      throw new Error(
        `company ${company.company_code} has no ${capability.code} result`,
      );
    }
    return [
      report.fiscal_year,
      report.quarter,
      report.through_period,
      report.generated_at,
      capability.name,
      company.company_code,
      company.company_name,
      STATUS_LABELS[result.status],
      result.outcome,
      result.alert_code ?? "",
      result.reason ?? "",
      result.evidence_limited === true ? "是" : "否",
      company.master_data_issues.join("；"),
      formatErrorMap(company.fetch_errors),
      formatErrorMap(company.adapter_errors),
      formatSourceStatus(company.source_status),
      result.candidates === undefined ? "" : JSON.stringify(result.candidates),
      ...valueKeys.map((key) => result.values[key] ?? ""),
    ];
  });
  return [headers, ...records]
    .map((record) => record.map(csvCell).join(","))
    .join("\r\n");
}

export function downloadValidationDetailsCsv(
  report: FullValidationReport,
  capability: Pick<CapabilityDefinition, "code" | "name" | "shortName">,
  companies: readonly ValidationCompany[],
): void {
  if (companies.length === 0) return;
  const csv = buildValidationDetailsCsv(report, capability, companies);
  const blob = new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = validationDetailsFilename(report, capability.shortName);
  anchor.style.display = "none";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export function validationDetailsFilename(
  report: FullValidationReport,
  capabilityShortName: string,
): string {
  const generated = new Date(report.generated_at);
  const stamp = Number.isNaN(generated.getTime())
    ? "unknown-time"
    : [
        generated.getUTCFullYear().toString().padStart(4, "0"),
        (generated.getUTCMonth() + 1).toString().padStart(2, "0"),
        generated.getUTCDate().toString().padStart(2, "0"),
        generated.getUTCHours().toString().padStart(2, "0"),
        generated.getUTCMinutes().toString().padStart(2, "0"),
        generated.getUTCSeconds().toString().padStart(2, "0"),
      ].join("");
  const safeName = capabilityShortName.replace(/[\\/:*?"<>|]/g, "_");
  return `所得税风险监测_${safeName}_${report.fiscal_year}Q${report.quarter}_${stamp}.csv`;
}

function collectValueKeys(
  capability: CapabilityCode,
  companies: readonly ValidationCompany[],
): string[] {
  const keys = new Set<string>();
  for (const company of companies) {
    const values = company.monitor_results[capability]?.values;
    if (values !== undefined) {
      Object.keys(values).forEach((key) => keys.add(key));
    }
  }
  const preferred = VALUE_ORDER[capability] ?? [];
  return [
    ...preferred.filter((key) => keys.delete(key)),
    ...Array.from(keys).sort((left, right) => left.localeCompare(right)),
  ];
}

function formatErrorMap(errors: Record<string, string> | undefined): string {
  if (errors === undefined) return "";
  return Object.entries(errors)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([source, code]) => `${source}：${code}`)
    .join("；");
}

function formatSourceStatus(
  statuses: Record<string, SourceStatus> | undefined,
): string {
  if (statuses === undefined) return "";
  return Object.entries(statuses)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([source, status]) => {
      const details: string[] = [status.status];
      if (status.record_count !== undefined)
        details.push(`记录数=${status.record_count}`);
      if (status.provenance !== undefined)
        details.push(`来源=${status.provenance}`);
      if (status.error_code !== undefined)
        details.push(`错误=${status.error_code}`);
      return `${source}：${details.join("/")}`;
    })
    .join("；");
}

function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  const decimal = /^-?\d+(?:\.\d+)?$/.test(text);
  const protectedText =
    !decimal && /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return `"${protectedText.replaceAll('"', '""')}"`;
}

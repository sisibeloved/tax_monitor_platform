import { describe, expect, it } from "vitest";

import type { CapabilityDefinition } from "./capabilities";
import {
  buildValidationDetailsCsv,
  validationDetailsFilename,
} from "./exportDetails";
import type { FullValidationReport, ValidationCompany } from "./types";

const capability: CapabilityDefinition = {
  code: "current_tax_accrual",
  name: "季度应计提所得税准确性检查",
  shortName: "季度所得税",
  frequency: "每季度",
  stage: "LIVE",
  description: "test",
  output: "test",
};

const report: FullValidationReport = {
  schema_version: 1,
  generated_at: "2026-07-22T03:34:36.000Z",
  fiscal_year: 2026,
  quarter: 2,
  through_period: 6,
  currency: "CNY",
  amount_scale: 2,
  source_mode: "REAL",
  company_scope: {
    base_record_count: 2,
    excluded_blank_company_count: 0,
    included_company_count: 2,
  },
  runtime: {
    parallelism: 12,
    cache: "MEMORY",
    external_fetch_seconds: 1,
    request_count: 12,
    request_error_count: 1,
  },
  refund_evidence_notice: "notice",
  monitor_summary: {},
  companies: [],
};

const companies: ValidationCompany[] = [
  {
    company_code: "3000",
    company_name: '=HYPERLINK("https://example.invalid")',
    master_data_issues: ["税率缺失"],
    fetch_errors: { dgc_hesi_invoice: "DGC_HTTP_ERROR" },
    adapter_errors: {},
    source_status: {
      dgc_sap_profit: {
        status: "DATA",
        record_count: 36,
        provenance: "LIVE",
      },
    },
    monitor_results: {
      current_tax_accrual: {
        status: "ALERT",
        outcome: "少计提",
        reason: null,
        alert_code: "UNDER_ACCRUED",
        values: {
          tax_rate: "0.25",
          current_quarter_should_accrue: "100.25",
          current_quarter_current_tax: "20",
          difference: "-80.25",
        },
      },
    },
  },
];

describe("validation detail CSV export", () => {
  it("exports exact filtered rows, source evidence, and all monitor values", () => {
    const csv = buildValidationDetailsCsv(report, capability, companies);

    expect(csv).toContain('"公司代码","公司名称","结果状态"');
    expect(csv).toContain('"3000","\'=HYPERLINK(""https://example.invalid"")"');
    expect(csv).toContain('"示警","少计提","UNDER_ACCRUED"');
    expect(csv).toContain('"dgc_hesi_invoice：DGC_HTTP_ERROR"');
    expect(csv).toContain('"dgc_sap_profit：DATA/记录数=36/来源=LIVE"');
    expect(csv).toContain(
      '"所得税税率（tax_rate）","本季度应计提（current_quarter_should_accrue）","SAP本季度计提（current_quarter_current_tax）","计提差异（difference）"',
    );
    expect(csv).toContain('"0.25","100.25","20","-80.25"');
  });

  it("uses the report period and generated timestamp in the filename", () => {
    expect(validationDetailsFilename(report, capability.shortName)).toBe(
      "所得税风险监测_季度所得税_2026Q2_20260722033436.csv",
    );
  });

  it("exports tax-adjustment formula values and candidate evidence", () => {
    const adjustmentCapability: CapabilityDefinition = {
      code: "tax_adjustment_account_accuracy",
      name: "纳税调增科目准确性检查",
      shortName: "调增科目准确性",
      frequency: "每月",
      stage: "LIVE",
      description: "test",
      output: "test",
    };
    const adjustmentCompany: ValidationCompany = {
      company_code: "3CC0",
      company_name: "杭州海亮研学旅行有限公司",
      master_data_issues: [],
      monitor_results: {
        tax_adjustment_account_accuracy: {
          status: "CLEAR",
          outcome: "存在候选但未达到调增门槛",
          reason: "按门槛规则不示警",
          values: {
            welfare_cumulative: "30510.23",
            salary_cumulative: "4375576.45",
            welfare_deduction_limit: "612580.7030",
            welfare_adjustment: "0",
            welfare_abnormal_candidate_count: "1",
          },
          candidates: [
            {
              candidate_no: "1",
              voucher_no: "1000000150",
              gl_account: "6600081100",
              account_name: "费用-福利费-工作餐费",
              header_text: "供应商公务接待",
              detail_text: "供应商公务接待",
              recommended_account: "业务招待费",
              recommendation_basis: "行项目摘要命中关键词：供应商",
            },
          ],
        },
      },
    };

    const csv = buildValidationDetailsCsv(report, adjustmentCapability, [
      adjustmentCompany,
    ]);

    expect(csv).toContain("福利费累计金额（welfare_cumulative）");
    expect(csv).toContain("福利费纳税调增额（welfare_adjustment）");
    expect(csv).toContain('"1000000150"');
    expect(csv).toContain("6600081100");
    expect(csv).toContain("供应商公务接待");
    expect(csv).toContain("业务招待费");
    expect(csv).toContain("行项目摘要命中关键词：供应商");
  });
});

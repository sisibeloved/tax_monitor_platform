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
      '"本季度应计提（current_quarter_should_accrue）","SAP本季度计提（current_quarter_current_tax）","计提差异（difference）"',
    );
    expect(csv).toContain('"100.25","20","-80.25"');
  });

  it("uses the report period and generated timestamp in the filename", () => {
    expect(validationDetailsFilename(report, capability.shortName)).toBe(
      "所得税风险监测_季度所得税_2026Q2_20260722033436.csv",
    );
  });
});

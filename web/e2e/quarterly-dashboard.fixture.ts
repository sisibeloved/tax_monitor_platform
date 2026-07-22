import type { Page } from "@playwright/test";

const RUN_ID = "60000000-0000-4000-8000-000000000001";
const STANDARD_COMPANY_ID = "60000000-0000-4000-8000-000000000002";
const ACCRUAL_DETECTION_ID = "60000000-0000-4000-8000-000000000003";
const DEFERRED_DETECTION_ID = "60000000-0000-4000-8000-000000000004";
const SNAPSHOT_ID = "60000000-0000-4000-8000-000000000005";
const RULE_VERSION_ID = "60000000-0000-4000-8000-000000000006";
const TAX_MASTER_VERSION_ID = "60000000-0000-4000-8000-000000000007";

function dashboardCompanies(standardCompanyCode: string) {
  return Array.from({ length: 105 }, (_, index) => {
    const suffix = String(index).padStart(3, "0");
    const blocked = index >= 103;
    return {
      company_id:
        index === 0
          ? STANDARD_COMPANY_ID
          : `61000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      company_code: index === 0 ? standardCompanyCode : `E2E-mock-${suffix}`,
      company_name:
        index === 0
          ? "E2E Quarterly Company 000"
          : `E2E Quarterly Company ${suffix}`,
      data_ready: true,
      execution_status: blocked ? "BLOCKED" : "SUCCEEDED",
      blocked_reason: blocked ? "季度数据未完整冻结" : null,
      risk_count: index < 103 ? 1 : 0,
    };
  });
}

function riskCase(
  standardCompanyCode: string,
  overrides: Record<string, unknown>,
) {
  return {
    id: `62000000-0000-4000-8000-${String(overrides.idSuffix).padStart(12, "0")}`,
    run_id: RUN_ID,
    company_id: STANDARD_COMPANY_ID,
    company_code: standardCompanyCode,
    company_name: "E2E Quarterly Company 000",
    calculation_status: "CALCULATED",
    tax_burden_rate: null,
    tax_burden_deviation: null,
    not_calculated_reason: null,
    currency: "CNY",
    amount_scale: 2,
    status: "NEW",
    priority: 1,
    assignee: null,
    row_version: 1,
    ...overrides,
    idSuffix: undefined,
  };
}

function lineage(seedToken: string) {
  return {
    snapshot: { id: SNAPSHOT_ID, checksum: "a".repeat(64) },
    tax_master_version: {
      version: "0123456789abcdefabcd-r2",
      source_file_name: "quarterly-master-mock.xlsx",
      imported_at: "2026-07-03T07:00:00Z",
    },
    rule_version: { version: "deferred-tax-loss-less-profit-reviewed" },
    sources: [
      {
        batch: {
          id: "60000000-0000-4000-8000-000000000008",
          source: "SAP",
          dataset_code: "sap-quarterly",
          source_batch_key: `e2e-${seedToken}-sap-quarterly`,
          payload_ref: "sap-quarterly.json",
          extraction_time: "2026-07-03T08:00:00Z",
        },
      },
    ],
    metrics: [],
  };
}

function detectionBase(monitoringType: string, detectionId: string) {
  return {
    id: detectionId,
    run_id: RUN_ID,
    company_id: STANDARD_COMPANY_ID,
    snapshot_id: SNAPSHOT_ID,
    rule_version_id: RULE_VERSION_ID,
    tax_master_version_id: TAX_MASTER_VERSION_ID,
    monitoring_type: monitoringType,
    calculation_status: "CALCULATED",
    rate_value: null,
    tax_burden_rate: null,
    tax_burden_deviation: null,
    currency: "CNY",
    amount_scale: 2,
    structured_output: {},
    not_calculated_reason: null,
  };
}

export async function installQuarterlyDashboardMock(
  page: Page,
  standardCompanyCode: string,
) {
  const seedToken = standardCompanyCode.slice(4, -4);
  const accrualFormula = {
    cumulative_profit: "10000000.00",
    received_dividends: "1000000.00",
    fair_value_change: "500000.00",
    loss_carryforward: "2000000.00",
    base_before_floor: "6500000.00",
    cumulative_base: "6500000.00",
    tax_rate: "0.25",
    cumulative_tax_payable: "1625000.00",
    prior_quarter_current_tax: "900000.00",
    current_quarter_should_accrue: "725000.00",
    current_quarter_current_tax: "700000.00",
    current_quarter_difference: "25000.00",
    rounding_mode: "ROUND_HALF_UP",
  };
  const deferredFormula = {
    loss_carryforward: "2000000.00",
    cumulative_profit: "10000000.00",
    deferred_tax_base_formula: "LOSS_MINUS_PROFIT",
    deferred_tax_base: "-8000000.00",
    deferred_tax_rate: "0.20",
    system_cumulative_deferred_tax: "-1600000.00",
    sap_cumulative_deferred_tax_expense: "2000000.00",
    current_year_deferred_tax_adjustment: "-3600000.00",
    rounding_mode: "ROUND_HALF_UP",
  };

  await page.route("**/api/v1/dashboard/quarterly?**", async (route) => {
    await route.fulfill({
      json: {
        fiscal_year: 2026,
        quarter: 2,
        run_id: RUN_ID,
        coverage_company_count: 105,
        data_ready_count: 105,
        blocked_count: 2,
        risk_company_count: 103,
        potential_tax_cost_total: "43775000.00",
        currency: "CNY",
        amount_scale: 2,
        monitoring_type_counts: {
          ACCRUAL_ACCURACY: 1,
          DEFERRED_TAX_ACCURACY: 1,
          TAX_BURDEN: 0,
          POTENTIAL_TAX_COST: 0,
        },
        companies: {
          total: 105,
          page: 1,
          page_size: 200,
          items: dashboardCompanies(standardCompanyCode),
        },
      },
    });
  });
  await page.route("**/api/v1/risk-cases?**", async (route) => {
    await route.fulfill({
      json: {
        total: 2,
        page: 1,
        page_size: 200,
        items: [
          riskCase(standardCompanyCode, {
            idSuffix: 1,
            latest_detection_id: ACCRUAL_DETECTION_ID,
            monitoring_type: "ACCRUAL_ACCURACY",
            input_amount: "700000.00",
            result_amount: "725000.00",
            difference_amount: "25000.00",
            rate_value: "0.25",
            formula_substitution: accrualFormula,
            alert_code: "CURRENT_TAX_UNDER_ACCRUED",
            risk_direction: "UNDER",
            risk_amount: "25000.00",
            risk_rate: null,
          }),
          riskCase(standardCompanyCode, {
            idSuffix: 2,
            latest_detection_id: DEFERRED_DETECTION_ID,
            monitoring_type: "DEFERRED_TAX_ACCURACY",
            input_amount: "2000000.00",
            result_amount: "-1600000.00",
            difference_amount: "-3600000.00",
            rate_value: "0.20",
            formula_substitution: deferredFormula,
            alert_code: "DEFERRED_TAX_TO_REVERSE",
            risk_direction: "REVERSE",
            risk_amount: "3600000.00",
            risk_rate: "0.20",
          }),
        ],
      },
    });
  });
  await page.route("**/api/v1/detections/**", async (route) => {
    const deferred = route.request().url().endsWith(DEFERRED_DETECTION_ID);
    await route.fulfill({
      json: deferred
        ? {
            ...detectionBase("DEFERRED_TAX_ACCURACY", DEFERRED_DETECTION_ID),
            input_amount: "2000000.00",
            result_amount: "-1600000.00",
            difference_amount: "-3600000.00",
            formula_substitution: deferredFormula,
            lineage: lineage(seedToken),
            alert_code: "DEFERRED_TAX_TO_REVERSE",
            direction: "REVERSE",
          }
        : {
            ...detectionBase("ACCRUAL_ACCURACY", ACCRUAL_DETECTION_ID),
            input_amount: "700000.00",
            result_amount: "725000.00",
            difference_amount: "25000.00",
            formula_substitution: accrualFormula,
            lineage: lineage(seedToken),
            alert_code: "CURRENT_TAX_UNDER_ACCRUED",
            direction: "UNDER",
          },
    });
  });
}

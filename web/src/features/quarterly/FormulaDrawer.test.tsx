import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FormulaDrawer } from "./FormulaDrawer";

const calculatedDetection = {
  id: "40000000-0000-4000-8000-000000000001",
  run_id: "10000000-0000-4000-8000-000000000001",
  company_id: "20000000-0000-4000-8000-000000000001",
  snapshot_id: "50000000-0000-4000-8000-000000000001",
  rule_version_id: "60000000-0000-4000-8000-000000000001",
  tax_master_version_id: "70000000-0000-4000-8000-000000000001",
  monitoring_type: "ACCRUAL_ACCURACY",
  calculation_status: "CALCULATED",
  input_amount: "700000.000000000000",
  result_amount: "725000.000000000000",
  difference_amount: "25000.000000000000",
  rate_value: "0.250000000000",
  tax_burden_rate: null,
  tax_burden_deviation: null,
  currency: "CNY",
  amount_scale: 2,
  formula_substitution: {
    currency: "CNY",
    amount_scale: 2,
    cumulative_profit: "10000000.000000000000",
    received_dividends: "1000000.000000000000",
    fair_value_change: "500000.000000000000",
    loss_carryforward: "2000000.000000000000",
    base_before_floor: "6500000.000000000000",
    cumulative_base: "6500000.000000000000",
    tax_rate: "0.250000000000",
    rounding_mode: "ROUND_HALF_UP",
    cumulative_tax_payable: "1625000.000000000000",
    prior_quarter_current_tax: "900000.000000000000",
    current_quarter_should_accrue: "725000.000000000000",
    current_quarter_current_tax: "700000.000000000000",
    current_quarter_difference: "25000.000000000000",
    cumulative_revenue: "50000000.000000000000",
    current_tax_burden: "0.032500000000",
    historical_average_tax_burden: "0.090000000000",
    tax_burden_deviation: "-0.057500000000",
    other_payables_accrual: "1400000.000000000000",
    hesi_no_invoice: "300000.000000000000",
    potential_adjustment: "1700000.000000000000",
    potential_base: "8200000.000000000000",
    potential_tax_payable: "2050000.000000000000",
    potential_tax_cost: "425000.000000000000",
  },
  lineage: {
    company: {
      id: "20000000-0000-4000-8000-000000000001",
      company_code: "1001",
    },
    snapshot: {
      id: "50000000-0000-4000-8000-000000000001",
      period: "2026-09-30",
      checksum: "snapshot-checksum-2026q3",
      source_version_set_hash: "source-set-2026q3",
      snapshot_set_id: "80000000-0000-4000-8000-000000000001",
    },
    rule_version: {
      id: "60000000-0000-4000-8000-000000000001",
      rule_code: "QUARTERLY_V1",
      version: "phase-1-reviewed",
      definition: {},
    },
    tax_master_version: {
      id: "70000000-0000-4000-8000-000000000001",
      version: "tax-master-2026-q3-v7",
      source_batch_id: "90000000-0000-4000-8000-000000000001",
      source_checksum: "master-checksum-v7",
      source_row_number: 18,
      valid_from: "2026-01-01",
      valid_to: null,
      tax_rate: "0.250000000000",
      loss_carryforward: "2000000.000000000000",
      historical_average_tax_burden: "0.090000000000",
      currency: "CNY",
      amount_scale: 2,
    },
    sources: [
      {
        batch: {
          id: "90000000-0000-4000-8000-000000000010",
          source: "SAP",
          dataset_code: "sap-quarterly-trial-balance",
          source_batch_key: "SAP-2026-Q3-1001",
          checksum: "sap-source-checksum",
        },
        target_subset: {
          company_code: "1001",
          metric_codes: [
            "cumulative_profit",
            "received_dividends",
            "fair_value_change",
            "cumulative_revenue",
            "prior_quarter_current_tax",
            "current_quarter_current_tax",
            "other_payables_accrual",
            "hesi_no_invoice",
          ],
        },
      },
    ],
    metrics: [
      ["cumulative_profit", "10000000.000000000000", "SAP利润总额"],
      ["received_dividends", "1000000.000000000000", "SAP收到分红"],
      ["fair_value_change", "500000.000000000000", "SAP公允价值变动"],
      ["cumulative_revenue", "50000000.000000000000", "SAP营业收入"],
      ["prior_quarter_current_tax", "900000.000000000000", "SAP以前季度计提"],
      ["current_quarter_current_tax", "700000.000000000000", "SAP本季度计提"],
      ["other_payables_accrual", "1400000.000000000000", "SAP暂估余额"],
      ["hesi_no_invoice", "300000.000000000000", "合思无票报销"],
    ].map(([metricCode, amount, sourceRecordKey], index) => ({
      metric_code: metricCode,
      amount,
      source_record: {
        id: `source-record-${index}`,
        batch_id: "90000000-0000-4000-8000-000000000010",
        source_record_key: sourceRecordKey,
        payload: { metric_code: metricCode, amount },
        lineage: { source_field: sourceRecordKey },
      },
    })),
  },
  structured_output: {
    monitor_type: "ACCRUAL_ACCURACY",
    calculation_status: "CALCULATED",
    alert: true,
    alert_code: "UNDER_ACCRUED",
    direction: "UNDER",
  },
  not_calculated_reason: null,
  alert_code: "UNDER_ACCRUED",
  direction: "UNDER",
};

const notCalculableDetection = {
  ...calculatedDetection,
  id: "40000000-0000-4000-8000-000000000002",
  monitoring_type: "TAX_BURDEN",
  calculation_status: "NOT_CALCULABLE",
  input_amount: "1625000.000000000000",
  result_amount: null,
  difference_amount: null,
  tax_burden_rate: null,
  tax_burden_deviation: null,
  formula_substitution: {
    ...calculatedDetection.formula_substitution,
    cumulative_revenue: "0.000000000000",
    current_tax_burden: null,
    tax_burden_deviation: null,
  },
  structured_output: {
    monitor_type: "TAX_BURDEN",
    calculation_status: "NOT_CALCULABLE",
    alert: false,
    alert_code: null,
    direction: null,
  },
  not_calculated_reason: "REVENUE_NON_POSITIVE",
  alert_code: null,
  direction: null,
};

function installDetectionFetch(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(rawUrl, window.location.origin);
      if (!url.pathname.startsWith("/api/v1/detections/")) {
        throw new Error(`Unexpected request: ${url.toString()}`);
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function renderDrawer(detectionId: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <FormulaDrawer detectionId={detectionId} open onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal(
    "ResizeObserver",
    class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const getComputedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    getComputedStyle(element),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("FormulaDrawer", () => {
  it("shows formula substitutions and immutable source versions", async () => {
    installDetectionFetch(calculatedDetection);
    renderDrawer(calculatedDetection.id);

    const drawer = await screen.findByRole("dialog", {
      name: "公式与数据血缘",
    });
    expect(
      await within(drawer).findByText("本年累计应纳税额"),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(/累计计税基础\s*=\s*max\s*\(/),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /本年累计应纳税额\s*=\s*累计计税基础\s*×\s*适用税率/,
      ),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /本季度应计提所得税额\s*=\s*本年累计应纳税额\s*-\s*以前季度SAP所得税计提/,
      ),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /本季度所得税计提差异\s*=\s*本季度应计提所得税额\s*-\s*本季度SAP所得税计提/,
      ),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("累计利润总额")).toBeInTheDocument();
    expect(within(drawer).getByText("¥10,000,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("累计收到分红")).toBeInTheDocument();
    expect(within(drawer).getByText("¥1,000,000.00")).toBeInTheDocument();
    expect(
      within(drawer).getByText("累计公允价值变动损益"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("¥500,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("可弥补以前年度亏损")).toBeInTheDocument();
    expect(within(drawer).getByText("¥2,000,000.00")).toBeInTheDocument();
    expect(within(drawer).getAllByText("¥6,500,000.00")).toHaveLength(2);
    expect(within(drawer).getByText("适用税率")).toBeInTheDocument();
    expect(within(drawer).getByText("25%")).toBeInTheDocument();
    expect(within(drawer).getByText("本年累计应纳税额")).toBeInTheDocument();
    expect(within(drawer).getByText("¥1,625,000.00")).toBeInTheDocument();
    expect(
      within(drawer).getByText("以前季度SAP所得税计提"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("¥900,000.00")).toBeInTheDocument();
    expect(
      within(drawer).getByText("本季度应计提所得税额"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("¥725,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("本季度SAP所得税计提")).toBeInTheDocument();
    expect(within(drawer).getByText("¥700,000.00")).toBeInTheDocument();
    expect(
      within(drawer).getByText("本季度所得税计提差异"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("+¥25,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("ROUND_HALF_UP")).toBeInTheDocument();

    expect(within(drawer).getByText("SAP")).toBeInTheDocument();
    expect(
      within(drawer).getByText("sap-quarterly-trial-balance"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("SAP-2026-Q3-1001")).toBeInTheDocument();
    expect(
      within(drawer).getByText("snapshot-checksum-2026q3"),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText("tax-master-2026-q3-v7"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("phase-1-reviewed")).toBeInTheDocument();
    for (const sourceField of [
      "SAP利润总额",
      "SAP收到分红",
      "SAP公允价值变动",
      "SAP营业收入",
      "SAP以前季度计提",
      "SAP本季度计提",
      "SAP暂估余额",
      "合思无票报销",
    ]) {
      expect(within(drawer).getByText(sourceField)).toBeInTheDocument();
    }
  });

  it("shows both frozen tax-burden formulas and every substituted input", async () => {
    const burdenDetection = {
      ...calculatedDetection,
      id: "40000000-0000-4000-8000-000000000003",
      monitoring_type: "TAX_BURDEN",
      input_amount: "1625000.000000000000",
      result_amount: null,
      difference_amount: null,
      tax_burden_rate: "0.032500000000",
      tax_burden_deviation: "-0.057500000000",
      alert_code: "TAX_BURDEN_DEVIATION",
      direction: "LOW",
    };
    installDetectionFetch(burdenDetection);
    renderDrawer(burdenDetection.id);

    const drawer = await screen.findByRole("dialog", {
      name: "公式与数据血缘",
    });
    expect(
      await within(drawer).findByText(
        /本年累计所得税税负率\s*=\s*本年累计应纳税额\s*÷\s*损益表累计营业收入/,
      ),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /本年累计税负率偏离度\s*=\s*本年累计所得税税负率\s*-\s*前三个完整年度平均税负率/,
      ),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("¥1,625,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("¥50,000,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("3.25%")).toBeInTheDocument();
    expect(within(drawer).getByText("9%")).toBeInTheDocument();
    expect(within(drawer).getByText("-5.75%")).toBeInTheDocument();
  });

  it("shows all four potential-risk estimate formulas and shared inputs", async () => {
    const potentialDetection = {
      ...calculatedDetection,
      id: "40000000-0000-4000-8000-000000000004",
      monitoring_type: "POTENTIAL_TAX_COST",
      input_amount: "1700000.000000000000",
      result_amount: "2050000.000000000000",
      difference_amount: "425000.000000000000",
      alert_code: "POTENTIAL_TAX_COST",
      direction: "INCREASE",
    };
    installDetectionFetch(potentialDetection);
    renderDrawer(potentialDetection.id);

    const drawer = await screen.findByRole("dialog", {
      name: "公式与数据血缘",
    });
    expect(
      await within(drawer).findByText(
        /潜在调增金额\s*=\s*其他应付款暂估余额\s*\+\s*合思无票报销金额/,
      ),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(/潜在计税基础\s*=\s*max\s*\(/),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /本年累计潜在应计提所得税额\s*=\s*潜在计税基础\s*×\s*适用税率/,
      ),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByText(
        /潜在风险估算\s*=\s*本年累计潜在应计提所得税额\s*-\s*本年累计应纳税额/,
      ),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("¥1,400,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("¥300,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("¥1,700,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("¥8,200,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("¥2,050,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("+¥425,000.00")).toBeInTheDocument();
    expect(within(drawer).getByText("潜在风险估算")).toBeInTheDocument();
    expect(
      within(drawer).getByText(/潜在税务成本.*不是最终纳税结论/),
    ).toBeInTheDocument();
  });

  it("shows a non-calculable reason for non-positive revenue and never a false zero result", async () => {
    installDetectionFetch(notCalculableDetection);
    renderDrawer(notCalculableDetection.id);

    const drawer = await screen.findByRole("dialog", {
      name: "公式与数据血缘",
    });
    expect(
      (await within(drawer).findAllByText("不可计算")).length,
    ).toBeGreaterThan(0);
    expect(
      within(drawer).getByText("累计营业收入小于或等于0，税负率不可计算"),
    ).toBeInTheDocument();
    expect(within(drawer).queryByText(/^¥0(?:\.00)?$/)).not.toBeInTheDocument();
  });
});

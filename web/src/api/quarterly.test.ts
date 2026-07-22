import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchAllQuarterlyDashboard,
  fetchAllQuarterlyRiskCases,
} from "./quarterly";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function dashboardCompany(index: number) {
  return {
    company_id: `company-${index}`,
    company_code: String(1000 + index),
    company_name: `公司${index}`,
    data_ready: true,
    execution_status: "SUCCEEDED",
    blocked_reason: null,
    risk_count: 0,
  };
}

function riskCase(index: number) {
  return {
    id: `case-${index}`,
    run_id: "run-1",
    company_id: `company-${index}`,
    company_code: String(1000 + index),
    company_name: `公司${index}`,
    latest_detection_id: `detection-${index}`,
    monitoring_type: "ACCRUAL_ACCURACY",
    calculation_status: "CALCULATED",
    input_amount: "700000.000000000000",
    result_amount: "725000.000000000000",
    difference_amount: "25000.000000000000",
    rate_value: "0.250000000000",
    tax_burden_rate: null,
    tax_burden_deviation: null,
    formula_substitution: {},
    not_calculated_reason: null,
    alert_code: "UNDER_ACCRUED",
    risk_direction: "UNDER",
    risk_amount: "25000.000000000000",
    risk_rate: null,
    currency: "CNY",
    amount_scale: 2,
    status: "NEW",
    priority: 3,
    assignee: null,
    row_version: 1,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("quarterly paginated API", () => {
  it("loads and merges every company and risk-case page", async () => {
    const requested: URL[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const raw = typeof input === "string" ? input : input.toString();
        const url = new URL(raw, window.location.origin);
        requested.push(url);
        const page = Number(url.searchParams.get("page"));
        if (url.pathname === "/api/v1/dashboard/quarterly") {
          const allCompanies = Array.from({ length: 205 }, (_, index) =>
            dashboardCompany(index),
          );
          return response({
            fiscal_year: 2026,
            quarter: 3,
            run_id: "run-1",
            coverage_company_count: 205,
            data_ready_count: 205,
            blocked_count: 0,
            risk_company_count: 105,
            potential_tax_cost_total: "425000.000000000000",
            currency: "CNY",
            amount_scale: 2,
            monitoring_type_counts: {
              ACCRUAL_ACCURACY: 105,
              DEFERRED_TAX_ACCURACY: 105,
              TAX_BURDEN: 105,
              POTENTIAL_TAX_COST: 105,
            },
            companies: {
              total: 205,
              page,
              page_size: 200,
              items: allCompanies.slice((page - 1) * 200, page * 200),
            },
          });
        }
        const allCases = Array.from({ length: 315 }, (_, index) =>
          riskCase(index),
        );
        return response({
          total: 315,
          page,
          page_size: 200,
          items: allCases.slice((page - 1) * 200, page * 200),
        });
      }),
    );

    const dashboard = await fetchAllQuarterlyDashboard(2026, 3);
    const cases = await fetchAllQuarterlyRiskCases(2026, 3);

    expect(dashboard.companies.items).toHaveLength(205);
    expect(dashboard.companies.items.at(-1)?.company_id).toBe("company-204");
    expect(cases.items).toHaveLength(315);
    expect(cases.items.at(-1)?.id).toBe("case-314");
    expect(
      requested
        .filter((url) => url.pathname === "/api/v1/dashboard/quarterly")
        .map((url) => url.searchParams.get("page")),
    ).toEqual(["1", "2"]);
    expect(
      requested
        .filter((url) => url.pathname === "/api/v1/risk-cases")
        .map((url) => url.searchParams.get("page")),
    ).toEqual(["1", "2"]);
  });

  it("rejects changing totals and duplicate rows instead of returning incomplete data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const raw = typeof input === "string" ? input : input.toString();
        const url = new URL(raw, window.location.origin);
        const page = Number(url.searchParams.get("page"));
        if (url.pathname === "/api/v1/dashboard/quarterly") {
          return response({
            fiscal_year: 2026,
            quarter: 3,
            run_id: "run-1",
            coverage_company_count: 201,
            data_ready_count: 201,
            blocked_count: 0,
            risk_company_count: 0,
            potential_tax_cost_total: "0.000000000000",
            currency: "CNY",
            amount_scale: 2,
            monitoring_type_counts: {
              ACCRUAL_ACCURACY: 0,
              DEFERRED_TAX_ACCURACY: 0,
              TAX_BURDEN: 0,
              POTENTIAL_TAX_COST: 0,
            },
            companies: {
              total: page === 1 ? 201 : 202,
              page,
              page_size: 200,
              items:
                page === 1
                  ? Array.from({ length: 200 }, (_, index) =>
                      dashboardCompany(index),
                    )
                  : [dashboardCompany(200)],
            },
          });
        }
        return response({
          total: 201,
          page,
          page_size: 200,
          items:
            page === 1
              ? Array.from({ length: 200 }, (_, index) => riskCase(index))
              : [riskCase(199)],
        });
      }),
    );

    await expect(fetchAllQuarterlyDashboard(2026, 3)).rejects.toThrow(
      /分页总数不一致/,
    );
    await expect(fetchAllQuarterlyRiskCases(2026, 3)).rejects.toThrow(
      /重复风险案件/,
    );
  });
});

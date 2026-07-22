import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../App";
import { QuarterlyDashboardPage } from "./QuarterlyDashboardPage";

const dashboardResponse = {
  fiscal_year: 2026,
  quarter: 3,
  run_id: "10000000-0000-4000-8000-000000000001",
  coverage_company_count: 105,
  data_ready_count: 103,
  blocked_count: 2,
  risk_company_count: 7,
  potential_tax_cost_total: "425000.000000000000",
  currency: "CNY",
  amount_scale: 2,
  monitoring_type_counts: {
    ACCRUAL_ACCURACY: 3,
    DEFERRED_TAX_ACCURACY: 2,
    TAX_BURDEN: 2,
    POTENTIAL_TAX_COST: 4,
  },
  companies: {
    total: 105,
    page: 1,
    page_size: 200,
    items: [
      {
        company_id: "20000000-0000-4000-8000-000000000001",
        company_code: "1001",
        company_name: "集团总部",
        data_ready: true,
        execution_status: "SUCCEEDED",
        blocked_reason: null,
        risk_count: 1,
      },
      {
        company_id: "20000000-0000-4000-8000-000000000002",
        company_code: "1002",
        company_name: "华东A公司",
        data_ready: false,
        execution_status: "BLOCKED",
        blocked_reason: "SAP凭证批次缺失",
        risk_count: 0,
      },
      {
        company_id: "20000000-0000-4000-8000-000000000003",
        company_code: "1003",
        company_name: "华南B公司",
        data_ready: false,
        execution_status: "BLOCKED",
        blocked_reason: "税务主数据缺失",
        risk_count: 0,
      },
      ...Array.from({ length: 102 }, (_, index) => ({
        company_id: `generated-company-${index}`,
        company_code: String(2000 + index),
        company_name: `测试公司${index}`,
        data_ready: true,
        execution_status: "SUCCEEDED",
        blocked_reason: null,
        risk_count: 0,
      })),
    ],
  },
};

const riskCaseResponse = {
  total: 2,
  page: 1,
  page_size: 200,
  items: [
    {
      id: "30000000-0000-4000-8000-000000000001",
      company_id: "20000000-0000-4000-8000-000000000001",
      company_code: "1001",
      company_name: "集团总部",
      latest_detection_id: "40000000-0000-4000-8000-000000000001",
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
      risk_direction: "UNDER",
      risk_amount: "25000.000000000000",
      risk_rate: null,
      currency: "CNY",
      amount_scale: 2,
      status: "NEW",
      priority: 3,
      assignee: null,
      row_version: 1,
    },
    {
      id: "30000000-0000-4000-8000-000000000002",
      company_id: "20000000-0000-4000-8000-000000000001",
      company_code: "1001",
      company_name: "集团总部",
      latest_detection_id: "40000000-0000-4000-8000-000000000002",
      monitoring_type: "DEFERRED_TAX_ACCURACY",
      calculation_status: "CALCULATED",
      input_amount: "2800000.000000000000",
      result_amount: "3000000.000000000000",
      difference_amount: "200000.000000000000",
      rate_value: "0.250000000000",
      tax_burden_rate: null,
      tax_burden_deviation: null,
      formula_substitution: {
        cumulative_profit: "10000000.000000000000",
        loss_carryforward: "2000000.000000000000",
        deferred_tax_rate: "0.250000000000",
      },
      not_calculated_reason: null,
      alert_code: "DEFERRED_TAX_TO_ACCRUE",
      risk_direction: "ACCRUE",
      risk_amount: "200000.000000000000",
      risk_rate: null,
      currency: "CNY",
      amount_scale: 2,
      status: "NEW",
      priority: 3,
      assignee: null,
      row_version: 1,
    },
  ],
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetchMock() {
  const requestedUrls: URL[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const rawUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(rawUrl, window.location.origin);
    requestedUrls.push(url);
    if (url.pathname === "/api/v1/dashboard/quarterly") {
      return jsonResponse({
        ...dashboardResponse,
        fiscal_year: Number(url.searchParams.get("fiscal_year")),
        quarter: Number(url.searchParams.get("quarter")),
      });
    }
    if (url.pathname === "/api/v1/risk-cases") {
      return jsonResponse(riskCaseResponse);
    }
    throw new Error(`Unexpected request: ${url.toString()}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return requestedUrls;
}

function renderPage(): { queryClient: QueryClient } {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <QuarterlyDashboardPage />
    </QueryClientProvider>,
  );
  return { queryClient };
}

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState({}, "", "/?fiscal_year=2026&quarter=3");
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

describe("QuarterlyDashboardPage", () => {
  it("shows the five quarterly monitoring summary cards", async () => {
    installFetchMock();
    renderPage();

    expect(await screen.findByText("覆盖公司")).toBeInTheDocument();
    expect(screen.getByText("105")).toBeInTheDocument();
    expect(screen.getByText("数据就绪")).toBeInTheDocument();
    expect(screen.getByText("103")).toBeInTheDocument();
    expect(screen.getByText("数据质量阻断")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("异常公司")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("潜在风险估算")).toBeInTheDocument();
    expect(screen.getByText("¥425,000.00")).toBeInTheDocument();
    expect(screen.queryByText("潜在税务成本")).not.toBeInTheDocument();
  });

  it("changes both query keys and requests when year and quarter change", async () => {
    const requestedUrls = installFetchMock();
    const user = userEvent.setup();
    const { queryClient } = renderPage();

    await waitFor(() => {
      expect(
        requestedUrls.some(
          (url) =>
            url.pathname === "/api/v1/dashboard/quarterly" &&
            url.searchParams.get("fiscal_year") === "2026" &&
            url.searchParams.get("quarter") === "3",
        ),
      ).toBe(true);
    });

    await user.click(await screen.findByRole("combobox", { name: "年度" }));
    const yearOptions = await screen.findAllByText("2025");
    await user.click(yearOptions.at(-1) as HTMLElement);
    await user.click(await screen.findByRole("combobox", { name: "季度" }));
    const quarterOptions = await screen.findAllByText("第四季度");
    await user.click(quarterOptions.at(-1) as HTMLElement);

    await waitFor(() => {
      expect(
        requestedUrls.some(
          (url) =>
            url.pathname === "/api/v1/dashboard/quarterly" &&
            url.searchParams.get("fiscal_year") === "2025" &&
            url.searchParams.get("quarter") === "4",
        ),
      ).toBe(true);
      expect(
        requestedUrls.some(
          (url) =>
            url.pathname === "/api/v1/risk-cases" &&
            url.searchParams.get("fiscal_year") === "2025" &&
            url.searchParams.get("quarter") === "4",
        ),
      ).toBe(true);
    });

    const queryKeys = queryClient
      .getQueryCache()
      .getAll()
      .map((query) => JSON.stringify(query.queryKey));
    expect(queryKeys).toContain(
      JSON.stringify(["quarterly-dashboard", 2025, 4]),
    );
    expect(queryKeys).toContain(
      JSON.stringify(["quarterly-risk-cases", 2025, 4]),
    );
    expect(window.location.search).toContain("fiscal_year=2025");
    expect(window.location.search).toContain("quarter=4");
  });

  it("separates blocked data-quality rows from risk rows and renders risk evidence", async () => {
    installFetchMock();
    renderPage();

    const blockedRegion = await screen.findByRole("region", {
      name: "数据质量阻断",
    });
    expect(within(blockedRegion).getByText("华东A公司")).toBeInTheDocument();
    expect(
      within(blockedRegion).getByText("SAP凭证批次缺失"),
    ).toBeInTheDocument();
    expect(
      within(blockedRegion).queryByText("集团总部"),
    ).not.toBeInTheDocument();

    const riskRegion = screen.getByRole("region", { name: "风险清单" });
    expect(within(riskRegion).getAllByText("集团总部")).toHaveLength(2);
    expect(
      within(riskRegion).getByText("所得税计提准确性"),
    ).toBeInTheDocument();
    expect(within(riskRegion).getByText("少计提")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥700,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥725,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("+¥25,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getAllByText("待处理")).toHaveLength(2);
    expect(
      within(riskRegion).getByText("递延所得税计提/转回准确性"),
    ).toBeInTheDocument();
    expect(within(riskRegion).getByText("应计提")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥2,800,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥3,000,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("+¥200,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥2,000,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("¥10,000,000.00")).toBeInTheDocument();
    expect(within(riskRegion).getByText("25%")).toBeInTheDocument();
    expect(within(riskRegion).queryByText("华东A公司")).not.toBeInTheDocument();
  });

  it("does not expose Agent or semantic-risk navigation", () => {
    installFetchMock();
    renderWithQuery(<App />);

    expect(
      screen.queryByRole("link", { name: /Agent/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /语义风险/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Agent/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/语义风险/)).not.toBeInTheDocument();
  });

  it("keeps period filters visible and offers retry when one query fails", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "temporary failure" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    expect(await screen.findByText("季度监测数据加载失败")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "年度" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "季度" })).toBeInTheDocument();
    const attempts = fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.length).toBeGreaterThan(attempts),
    );
  });
});

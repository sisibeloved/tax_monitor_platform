import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IncomeTaxRefundPage } from "./IncomeTaxRefundPage";
import type { IncomeTaxRefundResults } from "./types";

const results: IncomeTaxRefundResults = {
  refund_tax_year: 2025,
  scan_period: "2026-03",
  received_count: 4,
  not_received_count: 1,
  wrong_account_count: 2,
  ambiguous_count: 1,
  received: [
    {
      target_id: "10000000-0000-4000-8000-000000000001",
      company_id: "20000000-0000-4000-8000-000000000001",
      company_code: "3000",
      company_name: "海亮示例公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "120000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "CORRECT",
      account_family: "INCOME_TAX_EXPENSE",
      receipt_source: "SAP_MATCH",
      matched_amount: "120000.00",
      gl_account_code: "6801010000",
      gl_account_name: "所得税费用",
      document_number: "510001",
      line_item: "001",
      posting_date: "2026-03-08",
      alert_code: null,
      writeback_status: "SUCCEEDED",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000002",
      company_id: "20000000-0000-4000-8000-000000000002",
      company_code: "3560",
      company_name: "错误科目示例公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "80000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "WRONG_ACCOUNT",
      account_family: "OTHER_INCOME",
      receipt_source: "SAP_MATCH",
      matched_amount: "80000.00",
      gl_account_code: "6117990000",
      gl_account_name: "其他收益",
      document_number: "510002",
      line_item: "002",
      posting_date: "2026-03-12",
      alert_code: "REFUND_BOOKED_TO_WRONG_ACCOUNT",
      writeback_status: "PENDING",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000005",
      company_id: "20000000-0000-4000-8000-000000000005",
      company_code: "6000",
      company_name: "应交税费入账示例公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "60000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "WRONG_ACCOUNT",
      account_family: "TAXES_PAYABLE",
      receipt_source: "SAP_MATCH",
      matched_amount: "60000.00",
      gl_account_code: "2221130000",
      gl_account_name: "应交税费-企业所得税",
      document_number: "510003",
      line_item: "003",
      posting_date: "2026-03-15",
      alert_code: "REFUND_BOOKED_TO_WRONG_ACCOUNT",
      writeback_status: "PENDING",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000006",
      company_id: "20000000-0000-4000-8000-000000000006",
      company_code: "7000",
      company_name: "飞书手工登记公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "40000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "NOT_APPLICABLE",
      account_family: null,
      receipt_source: "LARK_MANUAL",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: null,
      writeback_status: null,
    },
  ],
  not_received: [
    {
      target_id: "10000000-0000-4000-8000-000000000003",
      company_id: "20000000-0000-4000-8000-000000000003",
      company_code: "4000",
      company_name: "尚未退税公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "50000.00",
      currency: "CNY",
      receipt_status: "NOT_RECEIVED",
      booking_status: "NOT_APPLICABLE",
      account_family: null,
      receipt_source: "SAP_MATCH",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: null,
      writeback_status: null,
    },
  ],
  ambiguous: [
    {
      target_id: "10000000-0000-4000-8000-000000000004",
      company_id: "20000000-0000-4000-8000-000000000004",
      company_code: "5000",
      company_name: "待人工确认公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "30000.00",
      currency: "CNY",
      receipt_status: "AMBIGUOUS",
      booking_status: "AMBIGUOUS",
      account_family: null,
      receipt_source: "SAP_MATCH",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: null,
      writeback_status: null,
    },
  ],
};

describe("IncomeTaxRefundPage", () => {
  beforeEach(() => {
    window.history.replaceState(
      {},
      "",
      "/?refund_tax_year=2025&scan_year=2026&scan_month=3",
    );
  });

  it("loads the selected period and separates receipt outcomes", async () => {
    const fetchMock = vi.fn(async () => Response.json(results));
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <IncomeTaxRefundPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("海亮示例公司")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/income-tax-refunds/results?refund_tax_year=2025&scan_year=2026&scan_month=3",
        expect.objectContaining({ method: "GET" }),
      ),
    );

    expect(
      screen
        .getByText("已退税", { selector: ".ant-statistic-title" })
        .closest(".ant-statistic"),
    ).toHaveTextContent("4");
    expect(
      screen.getAllByText("已退税", { selector: ".ant-tag" }),
    ).toHaveLength(4);
    expect(
      screen
        .getByText("入账科目错误", { selector: ".ant-statistic-title" })
        .closest(".ant-statistic"),
    ).toHaveTextContent("2");
    expect(screen.getByText("其他收益")).toHaveStyle({
      color: "rgb(255, 77, 79)",
    });
    expect(screen.getByText("其他收益").closest("tr")).toHaveStyle({
      backgroundColor: "#fff1f0",
    });
    expect(screen.getByText("已退税但入账至其他收益")).toBeInTheDocument();
    expect(screen.getByText("已退税但入账至应交税费")).toBeInTheDocument();
    expect(
      screen.getByText("已退税（飞书已登记，停止扫描）"),
    ).toBeInTheDocument();
    expect(screen.getByText("已同步")).toBeInTheDocument();
    expect(screen.getAllByText("待回写")).toHaveLength(2);

    await userEvent.click(screen.getByRole("tab", { name: "未退税 (1)" }));
    expect(await screen.findByText("尚未退税公司")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "待人工确认 (1)" }));
    expect(await screen.findByText("待人工确认公司")).toBeInTheDocument();
  }, 10_000);
});

afterEach(() => {
  window.history.replaceState({}, "", "/");
  vi.unstubAllGlobals();
});

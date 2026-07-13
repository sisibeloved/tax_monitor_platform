import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperationsDashboard } from "./OperationsDashboard";


describe("OperationsDashboard", () => {
  it("distinguishes data errors, technical failures, and tax risks with governed retry", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            generated_at: "2026-07-13T08:00:00Z",
            t_plus_2_deadline: "2026-07-15T08:00:00Z",
            delivery_status: "AT_RISK",
            can_retry: true,
            counters: {
              data_errors: 2,
              technical_failures: 1,
              tax_risks: 18,
              provider_failures: 1,
              evidence_backlog: 7,
            },
            link_coverage_ratio: 0.93,
            runs: [
              {
                run_id: "11111111-1111-1111-1111-111111111111",
                run_type: "MONTHLY_SEMANTIC",
                period: "2026-06",
                status: "PARTIAL_SUCCESS",
                queue_wait_seconds: 120,
                company_counts: { succeeded: 123, blocked: 2, failed: 1 },
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "RUNNING" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <OperationsDashboard />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("部分成功")).toBeInTheDocument();
    expect(screen.getByText("数据错误")).toBeInTheDocument();
    expect(screen.getByText("技术失败")).toBeInTheDocument();
    expect(screen.getByText("税务风险")).toBeInTheDocument();
    expect(screen.getByText("关联覆盖率")).toBeInTheDocument();
    expect(screen.getByText("证据积压")).toBeInTheDocument();
    expect(screen.getByText("交付时限预警")).toBeInTheDocument();
    expect(screen.queryByText(/公司名称|摘要|申请事由/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "重试失败公司" }));
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/operations/runs/11111111-1111-1111-1111-111111111111/retry",
      expect.objectContaining({ method: "POST" }),
    );
  });
});


afterEach(() => {
  vi.unstubAllGlobals();
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SapLinkCoveragePage } from "./SapLinkCoveragePage";

describe("SAP凭证关联覆盖页面", () => {
  it("解释独立覆盖状态且不把未关联SAP凭证交给语义Agent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          total: 1,
          items: [
            {
              coverage_id: "10000000-0000-4000-8000-000000000001",
              company_id: "20000000-0000-4000-8000-000000000001",
              company_code: "C001",
              company_name: "示例公司",
              period: "2032-03-31",
              document_number: "510002",
              line_item: "002",
              amount: "880.00",
              currency: "CNY",
              link_status: "UNLINKED",
              exact_evidence_link_id: null,
              evaluated_via_business_document: false,
              snapshot_id: "30000000-0000-4000-8000-000000000001",
            },
          ],
        }),
      ),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <SapLinkCoveragePage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("510002")).toBeInTheDocument();
    expect(screen.getByText("未关联前置单据")).toBeInTheDocument();
    expect(
      screen.getByText("仅形成覆盖观察，不进入Agent语义判断"),
    ).toBeInTheDocument();
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

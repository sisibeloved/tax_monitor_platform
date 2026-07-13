import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExportJobsPage } from "./ExportJobsPage";


describe("ExportJobsPage", () => {
  it("shows lifecycle metadata and only offers downloads for valid completed jobs", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                id: "11111111-1111-1111-1111-111111111111",
                export_type: "BUSINESS_ENTERTAINMENT",
                requester_subject: "group-user",
                company_ids: ["22222222-2222-2222-2222-222222222222"],
                normalized_filters: {},
                schema_version: "business-entertainment-root-cases-v1",
                status: "COMPLETED",
                row_count: 12,
                checksum: "a".repeat(64),
                object_key: "exports/safe.xlsx",
                failure_code: null,
                expires_at: "2099-07-14T00:00:00Z",
                created_at: "2026-07-13T00:00:00Z",
                completed_at: "2026-07-13T00:01:00Z",
              },
              {
                id: "33333333-3333-3333-3333-333333333333",
                export_type: "BUSINESS_ENTERTAINMENT",
                requester_subject: "group-user",
                company_ids: [],
                normalized_filters: {},
                schema_version: "business-entertainment-root-cases-v1",
                status: "EXPIRED",
                row_count: 3,
                checksum: "b".repeat(64),
                object_key: null,
                failure_code: null,
                expires_at: "2026-07-12T00:00:00Z",
                created_at: "2026-07-11T00:00:00Z",
                completed_at: "2026-07-11T00:01:00Z",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            url: "/api/v1/exports/11111111-1111-1111-1111-111111111111/content?token=safe",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const openMock = vi.fn();
    vi.stubGlobal("open", openMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <ExportJobsPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("已过期")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "安全下载" });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]);
    expect(openMock).toHaveBeenCalledWith(expect.stringContaining("/content?token=safe"), "_blank", "noopener,noreferrer");
  });
});


afterEach(() => {
  vi.unstubAllGlobals();
});

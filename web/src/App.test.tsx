import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

describe("App", () => {
  it("shows the platform heading", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    );

    const heading = screen.getByRole("heading", {
      name: "集团所得税风险监测",
    });
    expect(heading).toBeInTheDocument();
    expect(heading).toHaveStyle({
      fontSize: "24px",
      lineHeight: "32px",
      whiteSpace: "nowrap",
    });
  });

  it("opens income-tax refund monitoring from the primary navigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    );

    await userEvent.click(
      screen.getByRole("tab", {
        name: "所得税退税进度监控及入账科目准确性检查",
      }),
    );

    expect(
      screen.getByRole("heading", {
        name: "所得税退税进度监控及入账科目准确性检查",
        level: 2,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "退税所属年度" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "扫描月份" }),
    ).toBeInTheDocument();
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

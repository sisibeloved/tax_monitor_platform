import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const session = {
  authenticated: true as const,
  subject: "tax-user",
  display_name: "税务用户",
  avatar_url: null,
  auth_method: "password",
  roles: ["group-tax"],
  organization_path: "/group/tax",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe("App", () => {
  it("shows the authenticated platform heading and current user", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/session")) {
          return Promise.resolve(jsonResponse(session));
        }
        return new Promise<Response>(() => undefined);
      }),
    );

    renderApp();

    expect(
      await screen.findByRole("heading", { name: "集团所得税风险监测" }),
    ).toBeInTheDocument();
    expect(screen.getByText("税务用户")).toBeInTheDocument();
  });

  it("opens income-tax refund monitoring from the primary navigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/session")) {
          return Promise.resolve(jsonResponse(session));
        }
        return new Promise<Response>(() => undefined);
      }),
    );
    renderApp();

    await userEvent.click(
      await screen.findByRole("tab", {
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

  it("authenticates with an account and password", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/session")) {
          return Promise.resolve(
            jsonResponse({ detail: "Unauthenticated" }, 401),
          );
        }
        if (url.endsWith("/api/v1/auth/config")) {
          return Promise.resolve(
            jsonResponse({ password_enabled: true, feishu_enabled: true }),
          );
        }
        if (url.endsWith("/api/v1/auth/login") && init?.method === "POST") {
          return Promise.resolve(jsonResponse(session));
        }
        return new Promise<Response>(() => undefined);
      }),
    );
    renderApp();

    await userEvent.type(await screen.findByLabelText("账号"), "tax.user");
    await userEvent.type(screen.getByLabelText("密码"), "correct-password");
    await userEvent.click(screen.getByRole("button", { name: /^登\s*录$/ }));

    expect(
      await screen.findByRole("heading", { name: "集团所得税风险监测" }),
    ).toBeInTheDocument();
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

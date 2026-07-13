import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RiskListPage } from "./RiskListPage";

const caseId = "10000000-0000-4000-8000-000000000001";
const evidenceLinkId = "20000000-0000-4000-8000-000000000001";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RiskListPage />
    </QueryClientProvider>,
  );
}

describe("业务招待费风险页面", () => {
  it("筛选风险并展示待定位、证据、建议和精确关联解决", async () => {
    let workflowStatus = "PENDING_COMPANY_CONFIRMATION";
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") {
        if (url.endsWith(`/api/v1/risk-cases/${caseId}/actions`)) {
          expect(JSON.parse(String(init.body))).toEqual({
            action: "REQUEST_ADJUSTMENT",
            to_status: "PENDING_ADJUSTMENT",
            reason: "确认科目入账风险，转入改账处理",
          });
          workflowStatus = "PENDING_ADJUSTMENT";
          return Response.json({
            id: caseId,
            status: workflowStatus,
            assignee: "reviewer",
            row_version: 2,
          });
        }
        expect(JSON.parse(String(init.body))).toEqual({
          evidence_link_id: evidenceLinkId,
          expected_row_version: 1,
        });
        return Response.json({
          source_case_id: caseId,
          root_case_id: "30000000-0000-4000-8000-000000000001",
          evidence_link_id: evidenceLinkId,
          merged: true,
        });
      }
      if (url.includes(`/api/v1/risk-cases/${caseId}`)) {
        return Response.json({
          case_id: caseId,
          company_id: "40000000-0000-4000-8000-000000000001",
          company_code: "C001",
          company_name: "示例公司",
          monitoring_type: "WELFARE",
          fiscal_year: 2032,
          period: 3,
          status: workflowStatus,
          merged_into_case_id: null,
          canonical_source_record_id: "50000000-0000-4000-8000-000000000001",
          source_mode: "BUSINESS_DOCUMENT_UNLINKED",
          sap_link_status: "PENDING_LOCATION",
          sap_document_number: null,
          sap_line_item: null,
          sap_fiscal_year: 2032,
          current_account_code: "660203",
          current_account_name: "职工福利费",
          signed_amount: "1200.00",
          risk_amount: "1280.00",
          currency: "CNY",
          risk_amount_source: "BUSINESS_DOCUMENT",
          semantic_label: "MEETING_EXPENSE",
          confidence_tier: "HIGH",
          evidence_refs: [
            { field_name: "申请事由", quoted_text: "内部培训班会议餐" },
          ],
          recommended_account_ids: ["EMPLOYEE_EDUCATION"],
          rationale_summary: "更符合职工教育经费。",
          missing_evidence: [],
          rule_version_id: "rule-v1",
          model_version_id: "model-v1",
          prompt_version_id: "prompt-v1",
          case_library_version_id: "cases-v1",
          account_dictionary_version: "accounts-v1",
          workflow_note: "待定位SAP凭证",
          row_version: 1,
          resolution_evidence_links: [
            {
              evidence_link_id: evidenceLinkId,
              relation_quality: "EXACT",
              matched_field: "reference",
              sap_document_number: "510001",
              sap_line_item: "001",
            },
          ],
        });
      }
      return Response.json({
        total: 1,
        page: 1,
        page_size: 100,
        items: [
          {
            id: caseId,
            company_id: "40000000-0000-4000-8000-000000000001",
            company_code: "C001",
            company_name: "示例公司",
            monitoring_type: "BUSINESS_ENTERTAINMENT",
            risk_amount: "1280.00",
            currency: "CNY",
            status: "NEW",
            row_version: 1,
            fiscal_year: 2032,
            period: 3,
            source_mode: "BUSINESS_DOCUMENT_UNLINKED",
            sap_link_status: "PENDING_LOCATION",
            semantic_label: "MEETING_EXPENSE",
            confidence_tier: "HIGH",
            workflow_note: "待定位SAP凭证",
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();

    expect(await screen.findByText("待定位SAP凭证")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "来源模式" })).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "SAP关联状态" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "置信度" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "导出Excel" })).toHaveAttribute(
      "href",
      "/api/v1/exports/business-entertainment.xlsx",
    );

    await user.click(screen.getByRole("button", { name: "查看详情" }));
    expect(await screen.findByText("内部培训班会议餐")).toBeInTheDocument();
    expect(screen.getByText("660203 职工福利费")).toBeInTheDocument();
    expect(screen.getByText("1200.00 CNY")).toBeInTheDocument();
    expect(screen.getByText("model-v1")).toBeInTheDocument();
    expect(screen.getByText("EMPLOYEE_EDUCATION")).toBeInTheDocument();
    expect(screen.getByText("仅允许使用已持久化的精确关联")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "确认风险" }));
    expect(await screen.findByText("待改账")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/risk-cases/${caseId}/actions`,
      expect.objectContaining({ method: "POST" }),
    );

    await user.click(screen.getByRole("button", { name: "关联SAP凭证" }));
    expect(screen.getByText("510001 / 001（精确关联）")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认解决" }));

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/business-entertainment/risk-cases/${caseId}/resolve-to-sap`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

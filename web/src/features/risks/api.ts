import { queryOptions } from "@tanstack/react-query";

import { apiGet, apiPost } from "../../api/client";
import type {
  BusinessEntertainmentRiskDetail,
  BusinessEntertainmentRiskList,
  RiskCaseActionResponse,
  ResolveCaseResponse,
  RiskFilters,
  RiskReviewOutcome,
} from "./types";

export const businessEntertainmentRiskListKey = [
  "business-entertainment-risk-cases",
] as const;
export const businessEntertainmentCaseKey = (caseId: string | null) =>
  ["business-entertainment-risk-case", caseId] as const;

export function riskListQueryOptions(filters: RiskFilters) {
  return queryOptions({
    queryKey: [...businessEntertainmentRiskListKey, filters],
    queryFn: () =>
      apiGet<BusinessEntertainmentRiskList>("/api/v1/risk-cases", {
        monitoring_type: filters.monitoringType ?? "BUSINESS_ENTERTAINMENT",
        fiscal_year: filters.fiscalYear,
        period: filters.period,
        source_mode: filters.sourceMode,
        sap_link_status: filters.sapLinkStatus,
        confidence: filters.confidence,
        page: 1,
        page_size: 100,
      }),
  });
}

export function riskDetailQueryOptions(caseId: string | null) {
  return queryOptions({
    queryKey: businessEntertainmentCaseKey(caseId),
    queryFn: () => {
      if (caseId === null) {
        throw new Error("风险事项ID不能为空");
      }
      return apiGet<BusinessEntertainmentRiskDetail>(
        `/api/v1/risk-cases/${caseId}`,
      );
    },
    enabled: caseId !== null,
  });
}

export function resolveCaseToSap(
  caseId: string,
  evidenceLinkId: string,
  expectedRowVersion: number,
) {
  return apiPost<ResolveCaseResponse, object>(
    `/api/v1/business-entertainment/risk-cases/${caseId}/resolve-to-sap`,
    {
      evidence_link_id: evidenceLinkId,
      expected_row_version: expectedRowVersion,
    },
  );
}

const reviewActions: Record<
  RiskReviewOutcome,
  { action: string; to_status: string; reason: string }
> = {
  CONFIRM: {
    action: "REQUEST_ADJUSTMENT",
    to_status: "PENDING_ADJUSTMENT",
    reason: "确认科目入账风险，转入改账处理",
  },
  REJECT: {
    action: "SUBMIT_GROUP_REVIEW",
    to_status: "GROUP_REVIEW",
    reason: "公司对风险判断有异议，提交集团复核",
  },
  REQUEST_EVIDENCE: {
    action: "REQUEST_EVIDENCE",
    to_status: "EVIDENCE_REQUIRED",
    reason: "现有材料不足，要求补充证据",
  },
};

export function applyRiskReview(caseId: string, outcome: RiskReviewOutcome) {
  return apiPost<RiskCaseActionResponse, object>(
    `/api/v1/risk-cases/${caseId}/actions`,
    reviewActions[outcome],
  );
}

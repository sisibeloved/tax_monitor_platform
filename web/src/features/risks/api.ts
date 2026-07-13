import { queryOptions } from "@tanstack/react-query";

import { apiGet, apiPost } from "../../api/client";
import type {
  BusinessEntertainmentRiskDetail,
  BusinessEntertainmentRiskList,
  ResolveCaseResponse,
  RiskFilters,
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
        monitoring_type: "BUSINESS_ENTERTAINMENT",
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

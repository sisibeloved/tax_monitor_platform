import { queryOptions } from "@tanstack/react-query";

import { apiGet } from "../../api/client";
import type { SapLinkCoverageList } from "./types";

export const sapLinkCoverageQueryKey = (fiscalYear: number, period: number) =>
  ["business-entertainment-coverage", fiscalYear, period] as const;

export function sapLinkCoverageQueryOptions(fiscalYear: number, period: number) {
  return queryOptions({
    queryKey: sapLinkCoverageQueryKey(fiscalYear, period),
    queryFn: () =>
      apiGet<SapLinkCoverageList>(
        "/api/v1/business-entertainment/sap-link-coverage",
        { fiscal_year: fiscalYear, period },
      ),
  });
}

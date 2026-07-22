import { queryOptions } from "@tanstack/react-query";

import { ApiError, apiGet } from "../../api/client";
import type { IncomeTaxRefundResults } from "./types";

export const incomeTaxRefundResultsQueryKey = (
  refundTaxYear: number,
  scanYear: number,
  scanMonth: number,
) => ["income-tax-refund-results", refundTaxYear, scanYear, scanMonth] as const;

export function incomeTaxRefundResultsQueryOptions(
  refundTaxYear: number,
  scanYear: number,
  scanMonth: number,
) {
  return queryOptions({
    queryKey: incomeTaxRefundResultsQueryKey(
      refundTaxYear,
      scanYear,
      scanMonth,
    ),
    queryFn: () =>
      apiGet<IncomeTaxRefundResults>("/api/v1/income-tax-refunds/results", {
        refund_tax_year: refundTaxYear,
        scan_year: scanYear,
        scan_month: scanMonth,
      }),
    retry: (failureCount, error) =>
      error instanceof ApiError && error.status >= 500 && failureCount < 2,
    retryDelay: (attemptIndex) => Math.min(250 * 2 ** attemptIndex, 1_000),
  });
}

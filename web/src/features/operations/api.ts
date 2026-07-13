import { queryOptions } from "@tanstack/react-query";

import { apiGet, apiPost } from "../../api/client";


export interface OperationsRun {
  run_id: string;
  run_type: "QUARTERLY" | "MONTHLY_SEMANTIC";
  period: string;
  status: "PENDING" | "RUNNING" | "PARTIAL_SUCCESS" | "SUCCEEDED" | "FAILED";
  queue_wait_seconds: number;
  company_counts: {
    succeeded: number;
    blocked: number;
    failed: number;
  };
}

export interface OperationsSummary {
  generated_at: string;
  t_plus_2_deadline: string | null;
  delivery_status: "ON_TRACK" | "AT_RISK" | "OVERDUE" | "COMPLETED";
  can_retry: boolean;
  counters: {
    data_errors: number;
    technical_failures: number;
    tax_risks: number;
    provider_failures: number;
    evidence_backlog: number;
  };
  link_coverage_ratio: number | null;
  runs: OperationsRun[];
}

export const operationsSummaryKey = ["operations-summary"] as const;

export function operationsSummaryQueryOptions() {
  return queryOptions({
    queryKey: operationsSummaryKey,
    queryFn: () => apiGet<OperationsSummary>("/api/v1/operations/summary"),
    refetchInterval: 30_000,
  });
}

export function retryOperationsRun(runId: string) {
  return apiPost<{ status: string }, object>(
    `/api/v1/operations/runs/${runId}/retry`,
    {},
  );
}

import { queryOptions } from "@tanstack/react-query";

import { apiGet, apiPost } from "../../api/client";


export interface ExportJob {
  id: string;
  export_type: "BUSINESS_ENTERTAINMENT";
  requester_subject: string;
  company_ids: string[];
  normalized_filters: Record<string, unknown>;
  schema_version: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "EXPIRED";
  row_count: number | null;
  checksum: string | null;
  object_key: string | null;
  failure_code: string | null;
  expires_at: string;
  created_at: string;
  completed_at: string | null;
}


export const exportJobsKey = ["export-jobs"] as const;


export function exportJobsQueryOptions() {
  return queryOptions({
    queryKey: exportJobsKey,
    queryFn: () => apiGet<{ items: ExportJob[] }>("/api/v1/exports"),
    refetchInterval: 5_000,
  });
}


export function createBusinessEntertainmentExport() {
  return apiPost<ExportJob, object>("/api/v1/exports", {
    export_type: "BUSINESS_ENTERTAINMENT",
    filters: {},
  });
}


export function issueDownloadUrl(jobId: string) {
  return apiPost<{ url: string }, object>(
    `/api/v1/exports/${jobId}/download-url`,
    {},
  );
}

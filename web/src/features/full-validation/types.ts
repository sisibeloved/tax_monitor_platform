export type ValidationStatus = "ALERT" | "CLEAR" | "BLOCKED" | "NOT_APPLICABLE";

export interface MonitorResult {
  status: ValidationStatus;
  outcome: string;
  reason: string | null;
  alert_code?: string | null;
  evidence_limited?: boolean;
  values: Record<string, string | null>;
  candidates?: Record<string, string>[];
}

export interface SourceStatus {
  status: "DATA" | "NO_DATA" | "ERROR";
  record_count?: number;
  provenance?: string;
  error_code?: string;
}

export interface ValidationCompany {
  company_code: string;
  company_name: string;
  master_data_issues: string[];
  monitor_results: Record<string, MonitorResult>;
  source_status?: Record<string, SourceStatus>;
  adapter_errors?: Record<string, string>;
  fetch_errors?: Record<string, string>;
}

export interface MonitorSummary {
  name: string;
  total: number;
  ALERT: number;
  CLEAR: number;
  BLOCKED: number;
  NOT_APPLICABLE: number;
}

export interface FullValidationReport {
  schema_version: number;
  generated_at: string;
  fiscal_year: number;
  quarter: number;
  through_period: number;
  currency: string;
  amount_scale: number;
  source_mode: "REAL";
  company_scope: {
    base_record_count: number;
    excluded_blank_company_count: number;
    included_company_count: number;
  };
  runtime: {
    parallelism: number;
    cache: string;
    external_fetch_seconds: number;
    request_count: number;
    request_error_count: number;
  };
  refund_evidence_notice: string;
  monitor_summary: Record<string, MonitorSummary>;
  companies: ValidationCompany[];
}

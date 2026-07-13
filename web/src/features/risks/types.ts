export type MonitorType =
  | "ACCRUAL_ACCURACY"
  | "TAX_BURDEN"
  | "POTENTIAL_TAX_COST"
  | "BUSINESS_ENTERTAINMENT"
  | "WELFARE"
  | "DONATION";

export interface BusinessEntertainmentRiskCase {
  id: string;
  company_id: string;
  company_code: string;
  company_name: string;
  monitoring_type: MonitorType;
  risk_amount: string;
  currency: string;
  status: string;
  row_version: number;
  fiscal_year: number;
  period: number;
  source_mode: "SAP_LINKED" | "BUSINESS_DOCUMENT_UNLINKED";
  sap_link_status: "LINKED" | "PENDING_LOCATION";
  sap_document_number: string | null;
  sap_line_item: string | null;
  semantic_label: string;
  confidence_tier: "HIGH" | "MEDIUM" | "LOW";
  workflow_note: string;
}

export interface BusinessEntertainmentRiskList {
  total: number;
  page: number;
  page_size: number;
  items: BusinessEntertainmentRiskCase[];
}

export interface EvidenceReference {
  field_name: string;
  quoted_text: string;
}

export interface ResolutionEvidenceLink {
  evidence_link_id: string;
  relation_quality: "EXACT";
  matched_field: string;
  sap_document_number: string;
  sap_line_item: string;
}

export interface BusinessEntertainmentRiskDetail {
  case_id: string;
  company_id: string;
  company_code: string;
  company_name: string;
  monitoring_type: MonitorType;
  fiscal_year: number;
  period: number;
  status: string;
  merged_into_case_id: string | null;
  canonical_source_record_id: string;
  source_mode: "SAP_LINKED" | "BUSINESS_DOCUMENT_UNLINKED";
  sap_link_status: "LINKED" | "PENDING_LOCATION";
  sap_document_number: string | null;
  sap_line_item: string | null;
  sap_fiscal_year: number | null;
  current_account_code: string | null;
  current_account_name: string | null;
  signed_amount: string | null;
  risk_amount: string;
  currency: string;
  risk_amount_source: string;
  semantic_label: string;
  confidence_tier: string;
  evidence_refs: EvidenceReference[];
  recommended_account_ids: string[];
  rationale_summary: string;
  missing_evidence: string[];
  rule_version_id: string;
  model_version_id: string;
  prompt_version_id: string;
  case_library_version_id: string;
  account_dictionary_version: string;
  workflow_note: string;
  row_version: number;
  resolution_evidence_links: ResolutionEvidenceLink[];
}

export interface ResolveCaseResponse {
  source_case_id: string;
  root_case_id: string;
  evidence_link_id: string;
  merged: boolean;
}

export type RiskReviewOutcome = "CONFIRM" | "REJECT" | "REQUEST_EVIDENCE";

export interface RiskCaseActionResponse {
  id: string;
  status: string;
  assignee: string | null;
  row_version: number;
}

export interface RiskFilters {
  fiscalYear: number;
  period: number;
  sourceMode?: string;
  sapLinkStatus?: string;
  confidence?: string;
  monitoringType?: MonitorType;
}

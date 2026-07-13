export interface SapLinkCoverageItem {
  coverage_id: string;
  company_id: string;
  company_code: string;
  company_name: string;
  period: string;
  document_number: string;
  line_item: string;
  amount: string;
  currency: string;
  link_status: string;
  exact_evidence_link_id: string | null;
  evaluated_via_business_document: boolean;
  snapshot_id: string;
}

export interface SapLinkCoverageList {
  total: number;
  items: SapLinkCoverageItem[];
}

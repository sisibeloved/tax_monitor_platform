export type RefundReceiptStatus = "RECEIVED" | "NOT_RECEIVED" | "AMBIGUOUS";

export type RefundBookingStatus =
  "CORRECT" | "WRONG_ACCOUNT" | "NOT_APPLICABLE" | "AMBIGUOUS";

export type RefundWritebackStatus =
  "PENDING" | "PROCESSING" | "SUCCEEDED" | "FAILED" | null;

export type RefundAccountFamily =
  | "INCOME_TAX_EXPENSE"
  | "OTHER_INCOME"
  | "TAXES_PAYABLE";

export type RefundReceiptSource = "SAP_MATCH" | "LARK_MANUAL";

export interface IncomeTaxRefundItem {
  target_id: string;
  company_id: string;
  company_code: string;
  company_name: string;
  refund_tax_year: number;
  scan_period: string;
  expected_refund_amount: string;
  currency: string;
  receipt_status: RefundReceiptStatus;
  booking_status: RefundBookingStatus;
  account_family: RefundAccountFamily | null;
  receipt_source: RefundReceiptSource;
  matched_amount: string | null;
  gl_account_code: string | null;
  gl_account_name: string | null;
  document_number: string | null;
  line_item: string | null;
  posting_date: string | null;
  alert_code: string | null;
  writeback_status: RefundWritebackStatus;
}

export interface IncomeTaxRefundResults {
  refund_tax_year: number;
  scan_period: string;
  received_count: number;
  not_received_count: number;
  wrong_account_count: number;
  ambiguous_count: number;
  received: IncomeTaxRefundItem[];
  not_received: IncomeTaxRefundItem[];
  ambiguous: IncomeTaxRefundItem[];
}

export interface IncomeTaxRefundSelection {
  refundTaxYear: number;
  scanMonth: number;
}

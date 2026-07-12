export type MonitoringType =
  "ACCRUAL_ACCURACY" | "TAX_BURDEN" | "POTENTIAL_TAX_COST";

export type CalculationStatus = "CALCULATED" | "NOT_CALCULABLE" | "FAILED";

export type MonitoringRunCompanyStatus =
  "PENDING" | "RUNNING" | "RETRY_PENDING" | "SUCCEEDED" | "BLOCKED" | "FAILED";

export type RiskCaseStatus =
  | "NEW"
  | "ASSIGNED"
  | "PENDING_COMPANY_CONFIRMATION"
  | "PENDING_ADJUSTMENT"
  | "ADJUSTED_PENDING_REVIEW"
  | "GROUP_REVIEW"
  | "EVIDENCE_REQUIRED"
  | "CLOSED";

export interface DashboardCompany {
  company_id: string;
  company_code: string;
  company_name: string;
  data_ready: boolean;
  execution_status: MonitoringRunCompanyStatus;
  blocked_reason: string | null;
  risk_count: number;
}

export interface QuarterlyDashboard {
  fiscal_year: number;
  quarter: number;
  run_id: string;
  coverage_company_count: number;
  data_ready_count: number;
  blocked_count: number;
  risk_company_count: number;
  potential_tax_cost_total: string;
  currency: string;
  amount_scale: number;
  monitoring_type_counts: Record<MonitoringType, number>;
  companies: {
    total: number;
    page: number;
    page_size: number;
    items: DashboardCompany[];
  };
}

export interface RiskCaseItem {
  id: string;
  run_id: string;
  company_id: string;
  company_code: string;
  company_name: string;
  latest_detection_id: string | null;
  monitoring_type: MonitoringType;
  calculation_status: CalculationStatus;
  input_amount: string | null;
  result_amount: string | null;
  difference_amount: string | null;
  tax_burden_rate: string | null;
  tax_burden_deviation: string | null;
  not_calculated_reason: string | null;
  alert_code: string | null;
  risk_direction: string;
  risk_amount: string | null;
  risk_rate: string | null;
  currency: string;
  amount_scale: number;
  status: RiskCaseStatus;
  priority: number;
  assignee: string | null;
  row_version: number;
}

export interface RiskCaseList {
  total: number;
  page: number;
  page_size: number;
  items: RiskCaseItem[];
}

export interface DetectionLineage {
  company?: Record<string, unknown>;
  snapshot?: Record<string, unknown>;
  rule_version?: Record<string, unknown>;
  tax_master_version?: Record<string, unknown>;
  sources?: Array<Record<string, unknown>>;
  metrics?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface DetectionDetail {
  id: string;
  run_id: string;
  company_id: string;
  snapshot_id: string;
  rule_version_id: string;
  tax_master_version_id: string;
  monitoring_type: MonitoringType;
  calculation_status: CalculationStatus;
  input_amount: string | null;
  result_amount: string | null;
  difference_amount: string | null;
  rate_value: string | null;
  tax_burden_rate: string | null;
  tax_burden_deviation: string | null;
  currency: string;
  amount_scale: number;
  formula_substitution: Record<string, string | number | null>;
  lineage: DetectionLineage;
  structured_output: Record<string, unknown>;
  not_calculated_reason: string | null;
  alert_code: string | null;
  direction: string | null;
}

const CURRENCY_SYMBOLS: Readonly<Record<string, string>> = {
  CNY: "¥",
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
};

interface ParsedDecimal {
  negative: boolean;
  integer: string;
  fraction: string;
}

function parseDecimal(value: string): ParsedDecimal | null {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (match === null) {
    return null;
  }
  return {
    negative: match[1] === "-",
    integer: (match[2] ?? "0").replace(/^0+(?=\d)/, ""),
    fraction: match[3] ?? "",
  };
}

function scaledDigits(decimal: ParsedDecimal, scale: number): string {
  const fraction = decimal.fraction.padEnd(scale + 1, "0");
  const keptFraction = fraction.slice(0, scale);
  let scaled = BigInt(`${decimal.integer}${keptFraction}` || "0");
  if (fraction.charAt(scale) >= "5") {
    scaled += 1n;
  }
  return scaled.toString().padStart(scale + 1, "0");
}

export function formatMoney(
  value: string | null,
  currency: string,
  scale: number,
  showPositive = false,
): string {
  if (value === null) {
    return "—";
  }
  const decimal = parseDecimal(value);
  if (decimal === null || !Number.isInteger(scale) || scale < 0) {
    return "—";
  }
  const digits = scaledDigits(decimal, scale);
  const integerEnd = scale === 0 ? digits.length : digits.length - scale;
  const integer = digits.slice(0, integerEnd).replace(/^0+(?=\d)/, "");
  const fraction = scale === 0 ? "" : `.${digits.slice(integerEnd)}`;
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const isZero = /^0+$/.test(digits);
  const sign =
    decimal.negative && !isZero ? "-" : showPositive && !isZero ? "+" : "";
  const symbol = CURRENCY_SYMBOLS[currency] ?? `${currency} `;
  return `${sign}${symbol}${grouped}${fraction}`;
}

export function formatPercent(value: string | null, precision = 2): string {
  if (value === null) {
    return "—";
  }
  const decimal = parseDecimal(value);
  if (decimal === null) {
    return "—";
  }
  const shiftedFraction = decimal.fraction.padEnd(2, "0");
  const percentInteger =
    `${decimal.integer}${shiftedFraction.slice(0, 2)}`.replace(/^0+(?=\d)/, "");
  const remainder = shiftedFraction.slice(2);
  const percentDecimal: ParsedDecimal = {
    negative: decimal.negative,
    integer: percentInteger || "0",
    fraction: remainder,
  };
  const digits = scaledDigits(percentDecimal, precision);
  const integerEnd =
    precision === 0 ? digits.length : digits.length - precision;
  const integer = digits.slice(0, integerEnd).replace(/^0+(?=\d)/, "");
  const fraction =
    precision === 0 ? "" : digits.slice(integerEnd).replace(/0+$/, "");
  const isZero = /^0+$/.test(digits);
  const sign = decimal.negative && !isZero ? "-" : "";
  return `${sign}${integer}${fraction ? `.${fraction}` : ""}%`;
}

import { queryOptions } from "@tanstack/react-query";

import type {
  DetectionDetail,
  QuarterlyDashboard,
  RiskCaseList,
} from "../features/quarterly/types";
import { apiGet } from "./client";

const PAGE_SIZE = 200;

export class PaginationConsistencyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PaginationConsistencyError";
  }
}

export const quarterlyDashboardQueryKey = (
  fiscalYear: number,
  quarter: number,
) => ["quarterly-dashboard", fiscalYear, quarter] as const;

export const quarterlyRiskCasesQueryKey = (
  fiscalYear: number,
  quarter: number,
) => ["quarterly-risk-cases", fiscalYear, quarter] as const;

export const quarterlyDetectionQueryKey = (detectionId: string | null) =>
  ["quarterly-detection", detectionId] as const;

function assertPage(
  page: { total: number; page: number; page_size: number; items: unknown[] },
  expectedPage: number,
  expectedTotal: number,
  expectedPageSize: number,
) {
  if (page.total !== expectedTotal) {
    throw new PaginationConsistencyError(
      `分页总数不一致：期望${expectedTotal}，收到${page.total}`,
    );
  }
  if (page.page !== expectedPage || page.page_size !== expectedPageSize) {
    throw new PaginationConsistencyError(
      `分页元数据不一致：期望第${expectedPage}页/每页${expectedPageSize}条`,
    );
  }
  if (page.items.length > expectedPageSize) {
    throw new PaginationConsistencyError("分页返回条数超过约定页大小");
  }
}

function dashboardFingerprint(value: QuarterlyDashboard): string {
  return JSON.stringify({
    fiscal_year: value.fiscal_year,
    quarter: value.quarter,
    run_id: value.run_id,
    coverage_company_count: value.coverage_company_count,
    data_ready_count: value.data_ready_count,
    blocked_count: value.blocked_count,
    risk_company_count: value.risk_company_count,
    potential_tax_cost_total: value.potential_tax_cost_total,
    currency: value.currency,
    amount_scale: value.amount_scale,
    monitoring_type_counts: value.monitoring_type_counts,
  });
}

export async function fetchAllQuarterlyDashboard(
  fiscalYear: number,
  quarter: number,
): Promise<QuarterlyDashboard> {
  const getPage = (page: number) =>
    apiGet<QuarterlyDashboard>("/api/v1/dashboard/quarterly", {
      fiscal_year: fiscalYear,
      quarter,
      page,
      page_size: PAGE_SIZE,
    });
  const first = await getPage(1);
  const total = first.companies.total;
  const pageSize = first.companies.page_size;
  if (!Number.isInteger(total) || total < 0 || pageSize <= 0) {
    throw new PaginationConsistencyError("公司分页总数或页大小无效");
  }
  if (total !== first.coverage_company_count) {
    throw new PaginationConsistencyError(
      `公司覆盖总数不一致：汇总${first.coverage_company_count}，分页${total}`,
    );
  }
  assertPage(first.companies, 1, total, pageSize);
  const items = [...first.companies.items];
  const identity = dashboardFingerprint(first);
  const pageCount = Math.ceil(total / pageSize);
  for (let page = 2; page <= pageCount; page += 1) {
    const next = await getPage(page);
    if (dashboardFingerprint(next) !== identity) {
      throw new PaginationConsistencyError(
        "看板汇总数据在分页读取期间发生变化",
      );
    }
    assertPage(next.companies, page, total, pageSize);
    items.push(...next.companies.items);
  }
  if (items.length !== total) {
    throw new PaginationConsistencyError(
      `公司分页不完整：期望${total}条，收到${items.length}条`,
    );
  }
  const identities = new Set(items.map((item) => item.company_id));
  if (identities.size !== items.length) {
    throw new PaginationConsistencyError("公司分页包含重复公司");
  }
  return {
    ...first,
    companies: { ...first.companies, page: 1, items },
  };
}

export async function fetchAllQuarterlyRiskCases(
  fiscalYear: number,
  quarter: number,
): Promise<RiskCaseList> {
  const getPage = (page: number) =>
    apiGet<RiskCaseList>("/api/v1/risk-cases", {
      fiscal_year: fiscalYear,
      quarter,
      page,
      page_size: PAGE_SIZE,
    });
  const first = await getPage(1);
  const total = first.total;
  const pageSize = first.page_size;
  if (!Number.isInteger(total) || total < 0 || pageSize <= 0) {
    throw new PaginationConsistencyError("风险案件分页总数或页大小无效");
  }
  assertPage(first, 1, total, pageSize);
  const items = [...first.items];
  const pageCount = Math.ceil(total / pageSize);
  for (let page = 2; page <= pageCount; page += 1) {
    const next = await getPage(page);
    assertPage(next, page, total, pageSize);
    items.push(...next.items);
  }
  if (items.length !== total) {
    throw new PaginationConsistencyError(
      `风险案件分页不完整：期望${total}条，收到${items.length}条`,
    );
  }
  const identities = new Set(items.map((item) => item.id));
  if (identities.size !== items.length) {
    throw new PaginationConsistencyError("分页包含重复风险案件");
  }
  return { ...first, page: 1, items };
}

export function quarterlyDashboardQueryOptions(
  fiscalYear: number,
  quarter: number,
) {
  return queryOptions({
    queryKey: quarterlyDashboardQueryKey(fiscalYear, quarter),
    queryFn: () => fetchAllQuarterlyDashboard(fiscalYear, quarter),
  });
}

export function quarterlyRiskCasesQueryOptions(
  fiscalYear: number,
  quarter: number,
) {
  return queryOptions({
    queryKey: quarterlyRiskCasesQueryKey(fiscalYear, quarter),
    queryFn: () => fetchAllQuarterlyRiskCases(fiscalYear, quarter),
  });
}

export function quarterlyDetectionQueryOptions(detectionId: string | null) {
  return queryOptions({
    queryKey: quarterlyDetectionQueryKey(detectionId),
    queryFn: () => {
      if (detectionId === null) {
        throw new Error("A detection id is required");
      }
      return apiGet<DetectionDetail>(`/api/v1/detections/${detectionId}`);
    },
    enabled: detectionId !== null,
  });
}

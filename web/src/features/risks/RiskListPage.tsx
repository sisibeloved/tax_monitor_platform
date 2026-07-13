import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Select, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";

import { riskListQueryOptions } from "./api";
import { RiskDetailPage } from "./RiskDetailPage";
import type { BusinessEntertainmentRiskCase, RiskFilters } from "./types";

function currentPeriod(): Pick<RiskFilters, "fiscalYear" | "period"> {
  const now = new Date();
  return { fiscalYear: now.getFullYear(), period: now.getMonth() + 1 };
}

export function RiskListPage() {
  const [filters, setFilters] = useState<RiskFilters>(currentPeriod);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const cases = useQuery(riskListQueryOptions(filters));
  const columns: ColumnsType<BusinessEntertainmentRiskCase> = [
    {
      title: "公司",
      key: "company",
      render: (_, row) => `${row.company_code} ${row.company_name}`,
    },
    { title: "期间", key: "period", render: (_, row) => `${row.fiscal_year}-${row.period}` },
    { title: "判断标签", dataIndex: "semantic_label" },
    { title: "置信度", dataIndex: "confidence_tier" },
    {
      title: "SAP关联",
      key: "sap",
      render: (_, row) =>
        row.sap_link_status === "PENDING_LOCATION" ? (
          <Tag color="warning">待定位SAP凭证</Tag>
        ) : (
          <Tag color="success">已关联SAP凭证</Tag>
        ),
    },
    {
      title: "风险金额",
      key: "amount",
      render: (_, row) => `${row.risk_amount} ${row.currency}`,
    },
    {
      title: "操作",
      key: "actions",
      render: (_, row) => (
        <Button type="link" onClick={() => setSelectedCaseId(row.id)}>
          查看详情
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Space style={{ justifyContent: "space-between", width: "100%" }}>
        <Typography.Title level={2} style={{ margin: 0 }}>
          业务招待费风险清单
        </Typography.Title>
        <Button
          type="primary"
          href="/api/v1/exports/business-entertainment.xlsx"
          target="_blank"
        >
          导出Excel
        </Button>
      </Space>
      <Card size="small">
        <Space wrap>
          <Select
            aria-label="来源模式"
            placeholder="来源模式"
            allowClear
            style={{ width: 190 }}
            value={filters.sourceMode}
            options={[
              { label: "SAP已关联", value: "SAP_LINKED" },
              { label: "业务单据未关联", value: "BUSINESS_DOCUMENT_UNLINKED" },
            ]}
            onChange={(sourceMode) => setFilters({ ...filters, sourceMode })}
          />
          <Select
            aria-label="SAP关联状态"
            placeholder="SAP关联状态"
            allowClear
            style={{ width: 180 }}
            value={filters.sapLinkStatus}
            options={[
              { label: "已关联", value: "LINKED" },
              { label: "待定位", value: "PENDING_LOCATION" },
            ]}
            onChange={(sapLinkStatus) => setFilters({ ...filters, sapLinkStatus })}
          />
          <Select
            aria-label="置信度"
            placeholder="置信度"
            allowClear
            style={{ width: 140 }}
            value={filters.confidence}
            options={[
              { label: "高", value: "HIGH" },
              { label: "中", value: "MEDIUM" },
              { label: "低", value: "LOW" },
            ]}
            onChange={(confidence) => setFilters({ ...filters, confidence })}
          />
        </Space>
      </Card>
      {cases.isError ? <Alert type="error" message="风险清单加载失败" /> : null}
      <Table
        rowKey="id"
        loading={cases.isPending}
        columns={columns}
        dataSource={cases.data?.items ?? []}
        pagination={false}
        locale={{ emptyText: "当前筛选条件下无风险事项" }}
      />
      <RiskDetailPage
        caseId={selectedCaseId}
        onClose={() => setSelectedCaseId(null)}
      />
    </Space>
  );
}

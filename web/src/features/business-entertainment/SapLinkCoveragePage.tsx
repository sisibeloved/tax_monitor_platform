import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import { sapLinkCoverageQueryOptions } from "./api";
import type { SapLinkCoverageItem } from "./types";

function coverageMeaning(row: SapLinkCoverageItem) {
  if (row.link_status === "EXACT_LINKED") {
    return {
      label: "精确关联前置单据",
      color: "success",
      explanation: row.evaluated_via_business_document
        ? "已通过规范业务单据进入Agent判断"
        : "已建立精确证据关联",
    };
  }
  if (row.link_status === "FUZZY") {
    return {
      label: "仅模糊匹配",
      color: "warning",
      explanation: "相似关系不能自动挂接，需补充精确单据ID",
    };
  }
  return {
    label: "未关联前置单据",
    color: "default",
    explanation: "仅形成覆盖观察，不进入Agent语义判断",
  };
}

export function SapLinkCoveragePage() {
  const now = new Date();
  const coverage = useQuery(
    sapLinkCoverageQueryOptions(now.getFullYear(), now.getMonth() + 1),
  );
  const columns: ColumnsType<SapLinkCoverageItem> = [
    {
      title: "公司",
      key: "company",
      render: (_, row) => `${row.company_code} ${row.company_name}`,
    },
    { title: "期间", dataIndex: "period" },
    { title: "SAP凭证", dataIndex: "document_number" },
    { title: "行项目", dataIndex: "line_item" },
    {
      title: "金额",
      key: "amount",
      render: (_, row) => `${row.amount} ${row.currency}`,
    },
    {
      title: "覆盖状态",
      key: "status",
      render: (_, row) => {
        const meaning = coverageMeaning(row);
        return (
          <Space direction="vertical" size={0}>
            <Tag color={meaning.color}>{meaning.label}</Tag>
            <Typography.Text type="secondary">
              {meaning.explanation}
            </Typography.Text>
          </Space>
        );
      },
    },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Typography.Title level={2} style={{ margin: 0 }}>
        SAP凭证关联覆盖
      </Typography.Title>
      <Alert
        type="info"
        showIcon
        message="覆盖检查与语义判断相互独立"
        description="未找到精确前置单据的SAP凭证只进入覆盖清单，不交由Agent猜测业务性质。"
      />
      <Card>
        <Table
          rowKey="coverage_id"
          loading={coverage.isPending}
          columns={columns}
          dataSource={coverage.data?.items ?? []}
          pagination={false}
          locale={{ emptyText: "当前期间无SAP覆盖记录" }}
        />
      </Card>
    </Space>
  );
}

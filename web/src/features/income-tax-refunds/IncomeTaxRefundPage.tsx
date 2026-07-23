import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Col,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";

import { incomeTaxRefundResultsQueryOptions } from "./api";
import type {
  IncomeTaxRefundItem,
  IncomeTaxRefundSelection,
  RefundReceiptStatus,
  RefundWritebackStatus,
} from "./types";

const MONTH_OPTIONS = Array.from({ length: 10 }, (_, index) => {
  const month = index + 3;
  return { label: `${month}月`, value: month };
});

const receiptLabels: Record<
  RefundReceiptStatus,
  { label: string; color: string }
> = {
  RECEIVED: { label: "已退税", color: "success" },
  NOT_RECEIVED: { label: "尚未收到", color: "default" },
  AMBIGUOUS: { label: "多个等额候选示警", color: "error" },
};

const writebackLabels: Record<
  Exclude<RefundWritebackStatus, null>,
  { label: string; color: string }
> = {
  PENDING: { label: "待回写", color: "processing" },
  PROCESSING: { label: "回写中", color: "processing" },
  SUCCEEDED: { label: "已同步", color: "success" },
  FAILED: { label: "回写失败", color: "error" },
};

function defaultSelection(): IncomeTaxRefundSelection {
  const now = new Date();
  const currentMonth = now.getMonth() + 1;
  return {
    refundTaxYear: now.getFullYear() - 1,
    scanMonth: Math.min(12, Math.max(3, currentMonth)),
  };
}

function selectionFromUrl(): IncomeTaxRefundSelection {
  const fallback = defaultSelection();
  const search = new URLSearchParams(window.location.search);
  const refundTaxYear = Number(search.get("refund_tax_year"));
  const scanYear = Number(search.get("scan_year"));
  const scanMonth = Number(search.get("scan_month"));
  const validRefundYear =
    Number.isInteger(refundTaxYear) &&
    refundTaxYear >= 2000 &&
    refundTaxYear <= 9998 &&
    scanYear === refundTaxYear + 1;
  return {
    refundTaxYear: validRefundYear ? refundTaxYear : fallback.refundTaxYear,
    scanMonth:
      Number.isInteger(scanMonth) && scanMonth >= 3 && scanMonth <= 12
        ? scanMonth
        : fallback.scanMonth,
  };
}

function writeSelectionToUrl(selection: IncomeTaxRefundSelection) {
  const search = new URLSearchParams(window.location.search);
  search.set("refund_tax_year", String(selection.refundTaxYear));
  search.set("scan_year", String(selection.refundTaxYear + 1));
  search.set("scan_month", String(selection.scanMonth));
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}?${search.toString()}${window.location.hash}`,
  );
}

function formatAmount(value: string | null, currency: string): string {
  if (value === null) {
    return "—";
  }
  const match = /^([+-]?)(\d+)(\.\d+)?$/.exec(value.trim());
  if (match === null) {
    return `${currency} ${value}`;
  }
  const integer = (match[2] ?? "0").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${currency} ${match[1] ?? ""}${integer}${match[3] ?? ""}`;
}

function statusTag(status: RefundReceiptStatus) {
  const value = receiptLabels[status];
  return <Tag color={value.color}>{value.label}</Tag>;
}

function conclusionTag(item: IncomeTaxRefundItem) {
  if (item.receipt_source === "LARK_MANUAL") {
    return <Tag color="success">已退税（飞书已登记，停止扫描）</Tag>;
  }
  if (item.receipt_status === "NOT_RECEIVED") {
    return <Tag>未取得退税</Tag>;
  }
  if (item.receipt_status === "AMBIGUOUS") {
    return <Tag color="error">存在多个等额候选，需人工确认</Tag>;
  }
  if (item.account_family === "INCOME_TAX_EXPENSE") {
    return <Tag color="success">已退税且入账至所得税费用</Tag>;
  }
  if (item.account_family === "OTHER_INCOME") {
    return <Tag color="error">已退税但入账至其他收益</Tag>;
  }
  if (item.account_family === "TAXES_PAYABLE") {
    return <Tag color="error">已退税但入账至应交税费</Tag>;
  }
  return <Tag>已退税</Tag>;
}

function writebackTag(status: RefundWritebackStatus) {
  if (status === null) {
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  const value = writebackLabels[status];
  return <Tag color={value.color}>{value.label}</Tag>;
}

const columns: ColumnsType<IncomeTaxRefundItem> = [
  {
    title: "公司",
    key: "company",
    width: 220,
    fixed: "left",
    render: (_, item) => (
      <Space direction="vertical" size={0}>
        <Typography.Text code>{item.company_code}</Typography.Text>
        <Typography.Text>{item.company_name}</Typography.Text>
      </Space>
    ),
  },
  {
    title: "应退税金额",
    key: "expected_refund_amount",
    width: 150,
    align: "right",
    render: (_, item) =>
      formatAmount(item.expected_refund_amount, item.currency),
  },
  {
    title: "到账状态",
    dataIndex: "receipt_status",
    width: 130,
    render: (status: RefundReceiptStatus) => statusTag(status),
  },
  {
    title: "监测结论",
    key: "conclusion",
    width: 270,
    render: (_, item) => conclusionTag(item),
  },
  {
    title: "入账科目",
    key: "account",
    width: 250,
    render: (_, item) =>
      item.gl_account_code === null ? (
        <Typography.Text type="secondary">—</Typography.Text>
      ) : (
        <Space direction="vertical" size={0}>
          <Typography.Text
            code
            type={
              item.booking_status === "WRONG_ACCOUNT" ? "danger" : undefined
            }
          >
            {item.gl_account_code}
          </Typography.Text>
          <Typography.Text
            type={
              item.booking_status === "WRONG_ACCOUNT" ? "danger" : undefined
            }
          >
            {item.gl_account_name}
          </Typography.Text>
        </Space>
      ),
  },
  {
    title: "匹配金额",
    key: "matched_amount",
    width: 150,
    align: "right",
    render: (_, item) => formatAmount(item.matched_amount, item.currency),
  },
  {
    title: "SAP凭证",
    key: "document",
    width: 150,
    render: (_, item) =>
      item.document_number === null
        ? "—"
        : `${item.document_number} / ${item.line_item ?? "—"}`,
  },
  {
    title: "过账日期",
    dataIndex: "posting_date",
    width: 120,
    render: (value: string | null) => value ?? "—",
  },
  {
    title: "飞书状态",
    dataIndex: "writeback_status",
    width: 120,
    render: (status: RefundWritebackStatus) => writebackTag(status),
  },
];

function RefundTable(props: {
  items: IncomeTaxRefundItem[];
  emptyText: string;
}) {
  return (
    <Table<IncomeTaxRefundItem>
      rowKey="target_id"
      columns={columns}
      dataSource={props.items}
      pagination={false}
      scroll={{ x: 1560 }}
      locale={{ emptyText: props.emptyText }}
      onRow={(item) =>
        item.booking_status === "WRONG_ACCOUNT" ||
        item.booking_status === "AMBIGUOUS"
          ? { style: { backgroundColor: "#fff1f0" } }
          : {}
      }
    />
  );
}

export function IncomeTaxRefundPage() {
  const [selection, setSelection] = useState(selectionFromUrl);
  const scanYear = selection.refundTaxYear + 1;
  const query = useQuery(
    incomeTaxRefundResultsQueryOptions(
      selection.refundTaxYear,
      scanYear,
      selection.scanMonth,
    ),
  );

  const years = useMemo(() => {
    const currentRefundYear = new Date().getFullYear() - 1;
    return Array.from(
      new Set([
        selection.refundTaxYear,
        currentRefundYear,
        currentRefundYear - 1,
        currentRefundYear - 2,
        currentRefundYear - 3,
        currentRefundYear - 4,
      ]),
    )
      .filter((year) => year >= 2000 && year <= 9998)
      .sort((left, right) => right - left)
      .map((year) => ({ label: `${year}年`, value: year }));
  }, [selection.refundTaxYear]);

  const updateSelection = (next: IncomeTaxRefundSelection) => {
    writeSelectionToUrl(next);
    setSelection(next);
  };

  const data = query.data;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Row justify="space-between" align="middle" gutter={[16, 16]}>
        <Col>
          <Typography.Title level={2} style={{ margin: 0 }}>
            所得税退税进度监控及入账科目准确性检查
          </Typography.Title>
        </Col>
        <Col>
          <Space size="middle" wrap>
            <Select
              aria-label="退税所属年度"
              value={selection.refundTaxYear}
              options={years}
              onChange={(refundTaxYear) =>
                updateSelection({ ...selection, refundTaxYear })
              }
              style={{ width: 130 }}
            />
            <Space size="small">
              <Typography.Text type="secondary">{scanYear}年</Typography.Text>
              <Select
                aria-label="扫描月份"
                value={selection.scanMonth}
                options={MONTH_OPTIONS}
                onChange={(scanMonth) =>
                  updateSelection({ ...selection, scanMonth })
                }
                style={{ width: 100 }}
              />
            </Space>
          </Space>
        </Col>
      </Row>

      {query.isPending ? (
        <Alert type="info" showIcon message="正在加载退税监测结果" />
      ) : null}
      {query.isError ? (
        <Alert
          type="error"
          showIcon
          message="退税监测结果加载失败"
          action={
            <Button size="small" onClick={() => void query.refetch()}>
              重新加载
            </Button>
          }
        />
      ) : null}

      {data && !query.isError ? (
        <>
          <Row
            gutter={[0, 16]}
            style={{
              background: "#fff",
              border: "1px solid #f0f0f0",
              borderRadius: 4,
              padding: "16px 0",
            }}
          >
            <Col xs={12} lg={6} style={{ paddingInline: 20 }}>
              <Statistic title="已退税" value={data.received_count} />
            </Col>
            <Col xs={12} lg={6} style={{ paddingInline: 20 }}>
              <Statistic title="未退税" value={data.not_received_count} />
            </Col>
            <Col xs={12} lg={6} style={{ paddingInline: 20 }}>
              <Statistic
                title="入账科目错误"
                value={data.wrong_account_count}
                valueStyle={{ color: "#cf1322" }}
              />
            </Col>
            <Col xs={12} lg={6} style={{ paddingInline: 20 }}>
              <Statistic
                title="多个等额候选示警"
                value={data.ambiguous_count}
                valueStyle={{ color: "#d46b08" }}
              />
            </Col>
          </Row>

          <Tabs
            items={[
              {
                key: "received",
                label: `已退税 (${data.received_count})`,
                children: (
                  <RefundTable
                    items={data.received}
                    emptyText="当前期间无已退税公司"
                  />
                ),
              },
              {
                key: "not-received",
                label: `未退税 (${data.not_received_count})`,
                children: (
                  <RefundTable
                    items={data.not_received}
                    emptyText="当前期间无未退税公司"
                  />
                ),
              },
              {
                key: "ambiguous",
                label: `多个等额候选示警 (${data.ambiguous_count})`,
                children: (
                  <RefundTable
                    items={data.ambiguous}
                    emptyText="当前期间无多个等额候选示警"
                  />
                ),
              },
            ]}
          />
        </>
      ) : null}
    </Space>
  );
}

import { Button, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import type { RiskCaseItem } from "./types";
import { formatMoney, formatPercent } from "./types";

const MONITOR_LABELS: Readonly<
  Record<RiskCaseItem["monitoring_type"], string>
> = {
  ACCRUAL_ACCURACY: "所得税计提准确性",
  DEFERRED_TAX_ACCURACY: "递延所得税计提/转回准确性",
  TAX_BURDEN: "累计税负率异常",
  POTENTIAL_TAX_COST: "潜在风险估算",
};

const DIRECTION_LABELS: Readonly<Record<string, string>> = {
  UNDER: "少计提",
  OVER: "多计提",
  HIGH: "偏高",
  LOW: "偏低",
  INCREASE: "增加",
  DECREASE: "减少",
  ACCRUE: "应计提",
  REVERSE: "应转回",
};

const HIGH_RISK_DIRECTIONS = new Set(["UNDER", "INCREASE", "ACCRUE"]);

const STATUS_LABELS: Readonly<Record<RiskCaseItem["status"], string>> = {
  NEW: "待处理",
  ASSIGNED: "已分派",
  PENDING_COMPANY_CONFIRMATION: "待公司确认",
  PENDING_ADJUSTMENT: "待调整",
  ADJUSTED_PENDING_REVIEW: "已调整待复核",
  GROUP_REVIEW: "集团复核",
  EVIDENCE_REQUIRED: "待补证据",
  CLOSED: "已关闭",
};

function renderValue(
  item: RiskCaseItem,
  field: "input_amount" | "result_amount" | "difference_amount",
): string {
  if (item.calculation_status !== "CALCULATED") {
    return "不可计算";
  }
  if (item.monitoring_type === "TAX_BURDEN") {
    if (field === "input_amount") {
      return formatPercent(item.tax_burden_rate);
    }
    if (field === "difference_amount") {
      return formatPercent(item.tax_burden_deviation);
    }
    return "—";
  }
  return formatMoney(
    item[field],
    item.currency,
    item.amount_scale,
    field === "difference_amount",
  );
}

function renderDeferredMoney(item: RiskCaseItem, key: string): string {
  if (
    item.monitoring_type !== "DEFERRED_TAX_ACCURACY" ||
    item.calculation_status !== "CALCULATED"
  ) {
    return "—";
  }
  const value = item.formula_substitution?.[key];
  return formatMoney(
    typeof value === "string" ? value : null,
    item.currency,
    item.amount_scale,
  );
}

function renderDeferredRate(item: RiskCaseItem): string {
  if (
    item.monitoring_type !== "DEFERRED_TAX_ACCURACY" ||
    item.calculation_status !== "CALCULATED"
  ) {
    return "—";
  }
  const value = item.formula_substitution?.deferred_tax_rate;
  return formatPercent(typeof value === "string" ? value : item.rate_value);
}

export interface QuarterlyRunTableProps {
  items: RiskCaseItem[];
  loading?: boolean;
  onOpenDetection: (detectionId: string) => void;
}

export function QuarterlyRunTable({
  items,
  loading = false,
  onOpenDetection,
}: QuarterlyRunTableProps) {
  const columns: ColumnsType<RiskCaseItem> = [
    {
      title: "公司",
      key: "company",
      render: (_, item) => (
        <Space>
          <Typography.Text code>{item.company_code}</Typography.Text>
          <span>{item.company_name}</span>
        </Space>
      ),
    },
    {
      title: "风险类型",
      dataIndex: "monitoring_type",
      render: (value: RiskCaseItem["monitoring_type"]) => MONITOR_LABELS[value],
    },
    {
      title: "方向",
      dataIndex: "risk_direction",
      render: (value: string) => (
        <Tag color={HIGH_RISK_DIRECTIONS.has(value) ? "red" : "orange"}>
          {DIRECTION_LABELS[value] ?? value}
        </Tag>
      ),
    },
    {
      title: "账面/SAP值",
      key: "actual",
      render: (_, item) => renderValue(item, "input_amount"),
    },
    {
      title: "系统测算值",
      key: "expected",
      render: (_, item) => renderValue(item, "result_amount"),
    },
    {
      title: "差异/应计提转回",
      key: "difference",
      render: (_, item) => renderValue(item, "difference_amount"),
    },
    {
      title: "可弥补以前年度亏损",
      key: "loss_carryforward",
      render: (_, item) => renderDeferredMoney(item, "loss_carryforward"),
    },
    {
      title: "损益表累计利润总额",
      key: "cumulative_profit",
      render: (_, item) => renderDeferredMoney(item, "cumulative_profit"),
    },
    {
      title: "递延所得税税率",
      key: "deferred_tax_rate",
      render: (_, item) => renderDeferredRate(item),
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (value: RiskCaseItem["status"]) => (
        <Tag color={value === "CLOSED" ? "default" : "blue"}>
          {STATUS_LABELS[value]}
        </Tag>
      ),
    },
    {
      title: "证据",
      key: "evidence",
      render: (_, item) => (
        <Button
          type="link"
          disabled={item.latest_detection_id === null}
          onClick={() => {
            if (item.latest_detection_id !== null) {
              onOpenDetection(item.latest_detection_id);
            }
          }}
        >
          查看公式
        </Button>
      ),
    },
  ];

  return (
    <section aria-label="风险清单">
      <Table<RiskCaseItem>
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        locale={{ emptyText: "当前期间无风险案件" }}
        scroll={{ x: 1520 }}
      />
    </section>
  );
}

import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Descriptions,
  Drawer,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";
import type { ReactNode } from "react";

import { quarterlyDetectionQueryOptions } from "../../api/quarterly";
import type { DetectionDetail } from "./types";
import { formatMoney, formatPercent } from "./types";

const { Paragraph, Text, Title } = Typography;

const NOT_CALCULABLE_REASONS: Readonly<Record<string, string>> = {
  REVENUE_NON_POSITIVE: "累计营业收入小于或等于0，税负率不可计算",
  NON_POSITIVE_REVENUE: "累计营业收入小于或等于0，税负率不可计算",
  AMOUNT_OVERFLOW: "金额超出可计算范围",
  DECIMAL_CALCULATION_FAILED: "十进制计算失败",
};

function textValue(
  record: Record<string, unknown> | undefined,
  key: string,
): string | null {
  const value = record?.[key];
  return typeof value === "string" ? value : null;
}

function formulaValue(detection: DetectionDetail, key: string): string | null {
  const value = detection.formula_substitution[key];
  return typeof value === "string" ? value : null;
}

function moneyValue(detection: DetectionDetail, key: string, positive = false) {
  return formatMoney(
    formulaValue(detection, key),
    detection.currency,
    detection.amount_scale,
    positive,
  );
}

function AccrualFormula({ detection }: { detection: DetectionDetail }) {
  return (
    <>
      <Paragraph code>
        累计计税基础 = max(累计利润总额 - 累计收到分红 - 累计公允价值变动损益 -
        可弥补以前年度亏损, 0)
      </Paragraph>
      <Paragraph code>本年累计应纳税额 = 累计计税基础 × 适用税率</Paragraph>
      <Paragraph code>
        本季度应计提所得税额 = 本年累计应纳税额 - 以前季度SAP所得税计提
      </Paragraph>
      <Paragraph code>
        本季度所得税计提差异 = 本季度应计提所得税额 - 本季度SAP所得税计提
      </Paragraph>
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="累计利润总额">
          {moneyValue(detection, "cumulative_profit")}
        </Descriptions.Item>
        <Descriptions.Item label="累计收到分红">
          {moneyValue(detection, "received_dividends")}
        </Descriptions.Item>
        <Descriptions.Item label="累计公允价值变动损益">
          {moneyValue(detection, "fair_value_change")}
        </Descriptions.Item>
        <Descriptions.Item label="可弥补以前年度亏损">
          {moneyValue(detection, "loss_carryforward")}
        </Descriptions.Item>
        <Descriptions.Item label="取零前累计计税基础">
          {moneyValue(detection, "base_before_floor")}
        </Descriptions.Item>
        <Descriptions.Item label="累计计税基础">
          {moneyValue(detection, "cumulative_base")}
        </Descriptions.Item>
        <Descriptions.Item label="适用税率">
          {formatPercent(formulaValue(detection, "tax_rate"))}
        </Descriptions.Item>
        <Descriptions.Item label="本年累计应纳税额">
          {moneyValue(detection, "cumulative_tax_payable")}
        </Descriptions.Item>
        <Descriptions.Item label="以前季度SAP所得税计提">
          {moneyValue(detection, "prior_quarter_current_tax")}
        </Descriptions.Item>
        <Descriptions.Item label="本季度应计提所得税额">
          {moneyValue(detection, "current_quarter_should_accrue")}
        </Descriptions.Item>
        <Descriptions.Item label="本季度SAP所得税计提">
          {moneyValue(detection, "current_quarter_current_tax")}
        </Descriptions.Item>
        <Descriptions.Item label="本季度所得税计提差异">
          {moneyValue(detection, "current_quarter_difference", true)}
        </Descriptions.Item>
        <Descriptions.Item label="舍入模式">
          {formulaValue(detection, "rounding_mode") ?? "—"}
        </Descriptions.Item>
      </Descriptions>
    </>
  );
}

function TaxBurdenFormula({ detection }: { detection: DetectionDetail }) {
  const formulas = (
    <>
      <Paragraph code>
        本年累计所得税税负率 = 本年累计应纳税额 ÷ 损益表累计营业收入
      </Paragraph>
      <Paragraph code>
        本年累计税负率偏离度 = 本年累计所得税税负率 - 前三个完整年度平均税负率
      </Paragraph>
    </>
  );
  if (detection.calculation_status !== "CALCULATED") {
    const rawRevenue = formulaValue(detection, "cumulative_revenue");
    const reason = detection.not_calculated_reason ?? "UNKNOWN_REASON";
    return (
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {formulas}
        <Tag color="warning">不可计算</Tag>
        <Alert
          type="warning"
          showIcon
          message={NOT_CALCULABLE_REASONS[reason] ?? reason}
        />
        <Descriptions bordered size="small" column={1}>
          <Descriptions.Item label="本年累计营业收入">
            {rawRevenue === null ? "—" : `${rawRevenue}（≤0）`}
          </Descriptions.Item>
          <Descriptions.Item label="本年累计所得税税负率">
            不可计算
          </Descriptions.Item>
        </Descriptions>
      </Space>
    );
  }
  return (
    <>
      {formulas}
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="本年累计应纳税额">
          {moneyValue(detection, "cumulative_tax_payable")}
        </Descriptions.Item>
        <Descriptions.Item label="损益表累计营业收入">
          {moneyValue(detection, "cumulative_revenue")}
        </Descriptions.Item>
        <Descriptions.Item label="本年累计所得税税负率">
          {formatPercent(formulaValue(detection, "current_tax_burden"))}
        </Descriptions.Item>
        <Descriptions.Item label="前三个完整年度平均税负率">
          {formatPercent(
            formulaValue(detection, "historical_average_tax_burden"),
          )}
        </Descriptions.Item>
        <Descriptions.Item label="本年累计税负率偏离度">
          {formatPercent(formulaValue(detection, "tax_burden_deviation"))}
        </Descriptions.Item>
      </Descriptions>
    </>
  );
}

function PotentialFormula({ detection }: { detection: DetectionDetail }) {
  return (
    <>
      <Paragraph code>
        潜在调增金额 = 其他应付款暂估余额 + 合思无票报销金额
      </Paragraph>
      <Paragraph code>
        潜在计税基础 = max(累计利润总额 - 累计收到分红 - 累计公允价值变动损益 -
        可弥补以前年度亏损 + 潜在调增金额, 0)
      </Paragraph>
      <Paragraph code>
        本年累计潜在应计提所得税额 = 潜在计税基础 × 适用税率
      </Paragraph>
      <Paragraph code>
        潜在风险估算 = 本年累计潜在应计提所得税额 - 本年累计应纳税额
      </Paragraph>
      <Alert
        type="info"
        showIcon
        message="潜在风险估算用于反映潜在税务成本，不是最终纳税结论"
      />
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="累计利润总额">
          {moneyValue(detection, "cumulative_profit")}
        </Descriptions.Item>
        <Descriptions.Item label="累计收到分红">
          {moneyValue(detection, "received_dividends")}
        </Descriptions.Item>
        <Descriptions.Item label="累计公允价值变动损益">
          {moneyValue(detection, "fair_value_change")}
        </Descriptions.Item>
        <Descriptions.Item label="可弥补以前年度亏损">
          {moneyValue(detection, "loss_carryforward")}
        </Descriptions.Item>
        <Descriptions.Item label="适用税率">
          {formatPercent(formulaValue(detection, "tax_rate"))}
        </Descriptions.Item>
        <Descriptions.Item label="其他应付款暂估余额">
          {moneyValue(detection, "other_payables_accrual")}
        </Descriptions.Item>
        <Descriptions.Item label="合思无票报销金额">
          {moneyValue(detection, "hesi_no_invoice")}
        </Descriptions.Item>
        <Descriptions.Item label="潜在调增金额">
          {moneyValue(detection, "potential_adjustment")}
        </Descriptions.Item>
        <Descriptions.Item label="潜在计税基础">
          {moneyValue(detection, "potential_base")}
        </Descriptions.Item>
        <Descriptions.Item label="本年累计潜在应计提所得税额">
          {moneyValue(detection, "potential_tax_payable")}
        </Descriptions.Item>
        <Descriptions.Item label="本年累计应纳税额">
          {moneyValue(detection, "cumulative_tax_payable")}
        </Descriptions.Item>
        <Descriptions.Item label="潜在风险估算">
          {moneyValue(detection, "potential_tax_cost", true)}
        </Descriptions.Item>
      </Descriptions>
    </>
  );
}

function formulaContent(detection: DetectionDetail): ReactNode {
  if (detection.monitoring_type === "ACCRUAL_ACCURACY") {
    return <AccrualFormula detection={detection} />;
  }
  if (detection.monitoring_type === "TAX_BURDEN") {
    return <TaxBurdenFormula detection={detection} />;
  }
  return <PotentialFormula detection={detection} />;
}

function Lineage({ detection }: { detection: DetectionDetail }) {
  const snapshot = detection.lineage.snapshot;
  const master = detection.lineage.tax_master_version;
  const rule = detection.lineage.rule_version;
  const sources = detection.lineage.sources ?? [];
  const metrics = detection.lineage.metrics ?? [];
  const batches = new Map<string, Record<string, unknown>>();
  for (const source of sources) {
    const batch = source.batch;
    if (typeof batch === "object" && batch !== null && !Array.isArray(batch)) {
      const id = textValue(batch as Record<string, unknown>, "id");
      if (id !== null) {
        batches.set(id, batch as Record<string, unknown>);
      }
    }
  }
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Title level={5}>版本与来源</Title>
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="Snapshot ID">
          {textValue(snapshot, "id") ?? detection.snapshot_id}
        </Descriptions.Item>
        <Descriptions.Item label="Snapshot校验值">
          {textValue(snapshot, "checksum") ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="税务主数据版本">
          {textValue(master, "version") ?? detection.tax_master_version_id}
        </Descriptions.Item>
        <Descriptions.Item label="规则版本">
          {textValue(rule, "version") ?? detection.rule_version_id}
        </Descriptions.Item>
      </Descriptions>
      {sources.map((source, index) => (
        <Descriptions
          bordered
          size="small"
          column={1}
          key={`${index}-${textValue(source.batch as Record<string, unknown> | undefined, "id") ?? "source"}`}
        >
          <Descriptions.Item label="来源系统">
            {textValue(
              source.batch as Record<string, unknown> | undefined,
              "source",
            ) ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item label="数据集">
            {textValue(
              source.batch as Record<string, unknown> | undefined,
              "dataset_code",
            ) ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item label="来源批次">
            {textValue(
              source.batch as Record<string, unknown> | undefined,
              "source_batch_key",
            ) ?? "—"}
          </Descriptions.Item>
        </Descriptions>
      ))}
      <Title level={5}>输入项来源映射</Title>
      {metrics.map((metric, index) => {
        const sourceRecord = metric.source_record;
        const record =
          typeof sourceRecord === "object" &&
          sourceRecord !== null &&
          !Array.isArray(sourceRecord)
            ? (sourceRecord as Record<string, unknown>)
            : undefined;
        const batch = batches.get(textValue(record, "batch_id") ?? "");
        return (
          <Descriptions
            bordered
            size="small"
            column={1}
            key={`${index}-${textValue(metric, "metric_code") ?? "metric"}`}
          >
            <Descriptions.Item label="输入字段">
              {textValue(metric, "metric_code") ?? "—"}
            </Descriptions.Item>
            <Descriptions.Item label="来源字段/记录">
              {textValue(record, "source_record_key") ?? "—"}
            </Descriptions.Item>
            <Descriptions.Item label="来源系统与批次">
              {[
                textValue(batch, "source"),
                textValue(batch, "source_batch_key"),
              ]
                .filter((value): value is string => value !== null)
                .join(" / ") || "—"}
            </Descriptions.Item>
          </Descriptions>
        );
      })}
    </Space>
  );
}

export interface FormulaDrawerProps {
  detectionId: string | null;
  open: boolean;
  onClose: () => void;
}

export function FormulaDrawer({
  detectionId,
  open,
  onClose,
}: FormulaDrawerProps) {
  const detail = useQuery(quarterlyDetectionQueryOptions(detectionId));
  return (
    <Drawer
      title="公式与数据血缘"
      open={open}
      onClose={onClose}
      width={720}
      destroyOnClose
    >
      {detail.isPending ? <Skeleton active /> : null}
      {detail.isError ? (
        <Alert type="error" showIcon message="公式与数据血缘加载失败" />
      ) : null}
      {detail.data ? (
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <div>
            <Text type="secondary">
              监测计算由后端冻结规则执行，页面仅展示结果。
            </Text>
            {formulaContent(detail.data)}
          </div>
          <Lineage detection={detail.data} />
        </Space>
      ) : null}
    </Drawer>
  );
}

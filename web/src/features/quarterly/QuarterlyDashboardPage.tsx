import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";

import {
  quarterlyDashboardQueryOptions,
  quarterlyRiskCasesQueryOptions,
} from "../../api/quarterly";
import { FormulaDrawer } from "./FormulaDrawer";
import { QuarterlyRunTable } from "./QuarterlyRunTable";
import type { DashboardCompany } from "./types";
import { formatMoney } from "./types";

const { Title } = Typography;

interface PeriodSelection {
  fiscalYear: number;
  quarter: number;
}

function currentPeriod(): PeriodSelection {
  const now = new Date();
  return {
    fiscalYear: now.getFullYear(),
    quarter: Math.floor(now.getMonth() / 3) + 1,
  };
}

function periodFromUrl(): PeriodSelection {
  const fallback = currentPeriod();
  const search = new URLSearchParams(window.location.search);
  const fiscalYear = Number(search.get("fiscal_year"));
  const quarter = Number(search.get("quarter"));
  return {
    fiscalYear:
      Number.isInteger(fiscalYear) && fiscalYear >= 2000 && fiscalYear <= 9999
        ? fiscalYear
        : fallback.fiscalYear,
    quarter:
      Number.isInteger(quarter) && quarter >= 1 && quarter <= 4
        ? quarter
        : fallback.quarter,
  };
}

function writePeriodToUrl(period: PeriodSelection) {
  const search = new URLSearchParams(window.location.search);
  search.set("fiscal_year", String(period.fiscalYear));
  search.set("quarter", String(period.quarter));
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}?${search.toString()}${window.location.hash}`,
  );
}

const blockedColumns: ColumnsType<DashboardCompany> = [
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
    title: "数据状态",
    key: "status",
    render: () => <Tag color="error">阻断</Tag>,
  },
  {
    title: "原因",
    dataIndex: "blocked_reason",
    render: (value: string | null) => value ?? "数据未就绪",
  },
];

export function QuarterlyDashboardPage() {
  const [period, setPeriod] = useState(periodFromUrl);
  const [detectionId, setDetectionId] = useState<string | null>(null);
  const dashboard = useQuery(
    quarterlyDashboardQueryOptions(period.fiscalYear, period.quarter),
  );
  const riskCases = useQuery(
    quarterlyRiskCasesQueryOptions(period.fiscalYear, period.quarter),
  );

  const years = useMemo(() => {
    const currentYear = new Date().getFullYear();
    return Array.from(
      new Set([
        period.fiscalYear,
        currentYear + 1,
        currentYear,
        currentYear - 1,
        currentYear - 2,
        currentYear - 3,
      ]),
    )
      .sort((left, right) => right - left)
      .map((year) => ({ label: String(year), value: year }));
  }, [period.fiscalYear]);

  const updatePeriod = (next: PeriodSelection) => {
    writePeriodToUrl(next);
    setPeriod(next);
  };

  const isPending = dashboard.isPending || riskCases.isPending;
  const hasError = dashboard.isError || riskCases.isError;
  const dataQualityRows =
    dashboard.data?.companies.items.filter(
      (company) =>
        !company.data_ready || company.execution_status === "BLOCKED",
    ) ?? [];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Row justify="space-between" align="middle" gutter={[16, 16]}>
        <Col>
          <Title level={2} style={{ margin: 0 }}>
            季度所得税风险看板
          </Title>
        </Col>
        <Col>
          <Space size="middle">
            <Select
              aria-label="年度"
              value={period.fiscalYear}
              options={years}
              onChange={(fiscalYear) => updatePeriod({ ...period, fiscalYear })}
              style={{ width: 120 }}
            />
            <Select
              aria-label="季度"
              value={period.quarter}
              options={[
                { label: "第一季度", value: 1 },
                { label: "第二季度", value: 2 },
                { label: "第三季度", value: 3 },
                { label: "第四季度", value: 4 },
              ]}
              onChange={(quarter) => updatePeriod({ ...period, quarter })}
              style={{ width: 120 }}
            />
          </Space>
        </Col>
      </Row>

      {isPending ? (
        <Alert type="info" showIcon message="正在加载季度监测数据" />
      ) : null}
      {hasError ? (
        <Alert
          type="error"
          showIcon
          message="季度监测数据加载失败"
          action={
            <Button
              size="small"
              onClick={() => {
                void Promise.all([dashboard.refetch(), riskCases.refetch()]);
              }}
            >
              重新加载
            </Button>
          }
        />
      ) : null}

      {dashboard.data && riskCases.data && !hasError ? (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} sm={12} lg={4}>
              <Card>
                <Statistic
                  title="覆盖公司"
                  value={dashboard.data.coverage_company_count}
                />
              </Card>
            </Col>
            <Col xs={24} sm={12} lg={4}>
              <Card>
                <Statistic
                  title="数据就绪"
                  value={dashboard.data.data_ready_count}
                />
              </Card>
            </Col>
            <Col xs={24} sm={12} lg={4}>
              <Card>
                <Statistic
                  title="数据质量阻断"
                  value={dashboard.data.blocked_count}
                />
              </Card>
            </Col>
            <Col xs={24} sm={12} lg={4}>
              <Card>
                <Statistic
                  title="异常公司"
                  value={dashboard.data.risk_company_count}
                />
              </Card>
            </Col>
            <Col xs={24} sm={24} lg={8}>
              <Card>
                <Statistic
                  title="潜在风险估算"
                  value={dashboard.data.potential_tax_cost_total}
                  formatter={() =>
                    formatMoney(
                      dashboard.data.potential_tax_cost_total,
                      dashboard.data.currency,
                      dashboard.data.amount_scale,
                    )
                  }
                />
              </Card>
            </Col>
          </Row>

          <section aria-label="数据质量阻断">
            {dataQualityRows.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message={`${dataQualityRows.length}家公司因数据质量问题未进入风险计算`}
                style={{ marginBottom: 12 }}
              />
            ) : null}
            <Table<DashboardCompany>
              rowKey="company_id"
              columns={blockedColumns}
              dataSource={dataQualityRows}
              pagination={false}
              locale={{ emptyText: "当前期间无数据质量阻断" }}
            />
          </section>

          <QuarterlyRunTable
            items={riskCases.data.items}
            onOpenDetection={setDetectionId}
          />
          <FormulaDrawer
            detectionId={detectionId}
            open={detectionId !== null}
            onClose={() => setDetectionId(null)}
          />
        </>
      ) : null}
    </Space>
  );
}

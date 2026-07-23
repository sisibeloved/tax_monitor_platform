import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";

import {
  CAPABILITIES,
  LIVE_CAPABILITY_CODES,
  type CapabilityCode,
  type CapabilityDefinition,
  type CapabilityReadinessItem,
  type ReadinessStatus,
} from "./capabilities";
import {
  downloadValidationDetailsCsv,
  VALUE_LABELS,
  VALUE_ORDER,
} from "./exportDetails";
import type {
  FullValidationReport,
  MonitorResult,
  ValidationCompany,
  ValidationStatus,
} from "./types";

const STATUS_META: Record<ValidationStatus, { label: string; color: string }> =
  {
    ALERT: { label: "示警", color: "error" },
    CLEAR: { label: "正常", color: "success" },
    BLOCKED: { label: "阻断", color: "warning" },
    NOT_APPLICABLE: { label: "不适用", color: "default" },
  };

const STATUS_ORDER: ValidationStatus[] = [
  "ALERT",
  "CLEAR",
  "BLOCKED",
  "NOT_APPLICABLE",
];

const READINESS_META: Record<
  ReadinessStatus,
  { label: string; color: string }
> = {
  READY: { label: "已具备", color: "success" },
  PARTIAL: { label: "待接线", color: "processing" },
  MISSING: { label: "待补充", color: "warning" },
};

async function fetchReport(): Promise<FullValidationReport> {
  const response = await fetch(
    `${import.meta.env.BASE_URL}real-validation-latest.json?t=${Date.now()}`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(
      `full validation report request failed: ${response.status}`,
    );
  }
  return (await response.json()) as FullValidationReport;
}

function formatValue(
  key: string,
  value: string | null,
  report: FullValidationReport,
) {
  if (value === null) return "-";
  if (
    [
      "tax_rate",
      "deferred_tax_rate",
      "current_tax_burden",
      "historical_tax_burden",
      "deviation",
    ].includes(key)
  ) {
    const numeric = Number(value);
    return Number.isFinite(numeric)
      ? `${(numeric * 100).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}%`
      : value;
  }
  if (
    [
      "match_count",
      "booking_account",
      "booking_account_family",
      "match_stage",
      "receipt_source",
      "welfare_abnormal_candidate_count",
      "welfare_alert_count",
      "donation_abnormal_candidate_count",
      "donation_alert_count",
    ].includes(key)
  ) {
    return value;
  }
  if (key === "welfare_detail_selected") {
    return value === "true" ? "是" : "否";
  }
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: report.currency,
        minimumFractionDigits: report.amount_scale,
        maximumFractionDigits: report.amount_scale,
      }).format(numeric)
    : value;
}

const CANDIDATE_COLUMNS: ColumnsType<Record<string, string>> = [
  {
    title: "分类",
    width: 110,
    render: (_, candidate) => candidate.subject ?? candidate.family ?? "-",
  },
  {
    title: "期间 / 凭证号",
    width: 180,
    render: (_, candidate) => (
      <Space direction="vertical" size={0}>
        <Typography.Text>{candidate.fiscal_period ?? "-"}</Typography.Text>
        <Typography.Text code>{candidate.voucher_no ?? "-"}</Typography.Text>
      </Space>
    ),
  },
  {
    title: "当前入账科目",
    width: 240,
    render: (_, candidate) => (
      <Space direction="vertical" size={0}>
        <Typography.Text code>
          {candidate.gl_account ?? candidate.account_code ?? "-"}
        </Typography.Text>
        <Typography.Text>
          {candidate.account_name ?? candidate.family ?? "-"}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: "建议入账科目",
    width: 260,
    render: (_, candidate) => (
      <Space direction="vertical" size={0}>
        <Typography.Text strong>
          {candidate.recommended_account ?? "-"}
        </Typography.Text>
        <Typography.Text type="secondary">
          具体SAP科目编码按公司科目表确认
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: "摘要",
    width: 360,
    render: (_, candidate) => (
      <Space direction="vertical" size={0}>
        <Typography.Text>抬头：{candidate.header_text || "-"}</Typography.Text>
        <Typography.Text>
          行项目：{candidate.detail_text || "-"}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: "金额",
    width: 110,
    render: (_, candidate) => candidate.amount ?? "-",
  },
  {
    title: "识别结果 / 判断依据",
    width: 280,
    render: (_, candidate) => (
      <Space direction="vertical" size={0}>
        <Typography.Text>{candidate.classification ?? "-"}</Typography.Text>
        {candidate.matched_keywords ? (
          <Typography.Text type="secondary">
            关键词：{candidate.matched_keywords}
          </Typography.Text>
        ) : null}
        <Typography.Text type="secondary">
          {candidate.recommendation_basis ?? "-"}
        </Typography.Text>
      </Space>
    ),
  },
];

function CapabilityCard({
  capability,
  report,
  onSelect,
}: {
  capability: CapabilityDefinition;
  report: FullValidationReport;
  onSelect: (code: CapabilityCode) => void;
}) {
  const summary = report.monitor_summary[capability.code];
  const isLive = capability.stage === "LIVE";

  return (
    <Card
      size="small"
      style={{ height: "100%", borderRadius: 6 }}
      styles={{ body: { height: "100%" } }}
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <Row justify="space-between" align="top" wrap={false} gutter={8}>
          <Col flex="auto">
            <Typography.Text strong>{capability.name}</Typography.Text>
          </Col>
          <Col flex="none">
            <Tag color={isLive ? "success" : "warning"}>
              {isLive ? "已运行" : "待完善"}
            </Tag>
          </Col>
        </Row>
        <Typography.Text type="secondary">
          {capability.frequency} · {capability.description}
        </Typography.Text>
        {isLive ? (
          <Row align="bottom" justify="space-between" gutter={12}>
            <Col>
              <Statistic
                title="示警"
                value={summary?.ALERT ?? 0}
                suffix="家"
                valueStyle={{
                  color: (summary?.ALERT ?? 0) > 0 ? "#cf1322" : "#237804",
                }}
              />
            </Col>
            <Col>
              <Typography.Text type="secondary">
                正常 {summary?.CLEAR ?? 0}
              </Typography.Text>
              <br />
              <Typography.Text type="secondary">
                阻断 {summary?.BLOCKED ?? 0}
              </Typography.Text>
            </Col>
          </Row>
        ) : (
          <div style={{ minHeight: 65 }}>
            <Typography.Title level={4} style={{ margin: "4px 0" }}>
              未执行
            </Typography.Title>
            <Typography.Text type="secondary">
              当前仅展示建设状态，不计入正常率。
            </Typography.Text>
          </div>
        )}
        <Button
          type="link"
          style={{ padding: 0 }}
          onClick={() => onSelect(capability.code)}
        >
          {isLive ? "查看公司明细" : "查看建设状态"}
        </Button>
      </Space>
    </Card>
  );
}

function ReadinessPanel({ capability }: { capability: CapabilityDefinition }) {
  const columns: ColumnsType<CapabilityReadinessItem> = [
    { title: "数据或能力环节", dataIndex: "item", width: 220 },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (status: ReadinessStatus) => {
        const meta = READINESS_META[status];
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: "当前情况", dataIndex: "detail" },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert
        type="warning"
        showIcon
        message={`${capability.name}当前不生成监测结论`}
        description={capability.unavailableReason}
      />
      <Table<CapabilityReadinessItem>
        rowKey="item"
        columns={columns}
        dataSource={capability.readiness ?? []}
        pagination={false}
        scroll={{ x: 760 }}
        size="middle"
      />
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
        计划输出：{capability.output}
        。所有必需数据完成真实接口验收前，不生成“正常”或“示警”公司清单。
      </Typography.Paragraph>
    </Space>
  );
}

export function FullValidationPage() {
  const query = useQuery({
    queryKey: ["real-full-validation"],
    queryFn: fetchReport,
  });
  const [monitor, setMonitor] = useState<CapabilityCode>("current_tax_accrual");
  const [status, setStatus] = useState<ValidationStatus | "ALL">("ALL");
  const [outcome, setOutcome] = useState("ALL");
  const [keyword, setKeyword] = useState("");
  const report = query.data;
  const selectedCapability =
    CAPABILITIES.find((capability) => capability.code === monitor) ??
    CAPABILITIES[0];
  const monitorIsLive = selectedCapability.stage === "LIVE";

  const statusOptions = useMemo(() => {
    if (!report || !monitorIsLive) return [];
    const available = new Set(
      report.companies
        .map((company) => company.monitor_results[monitor]?.status)
        .filter((value): value is ValidationStatus => value !== undefined),
    );
    return STATUS_ORDER.filter((value) => available.has(value));
  }, [monitor, monitorIsLive, report]);

  const outcomeOptions = useMemo(() => {
    if (!report || !monitorIsLive) return [];
    return Array.from(
      new Set(
        report.companies
          .map((company) => company.monitor_results[monitor])
          .filter(
            (result): result is MonitorResult =>
              result !== undefined &&
              (status === "ALL" || result.status === status),
          )
          .map((result) => result.outcome.trim())
          .filter((value): value is string => Boolean(value)),
      ),
    ).sort((left, right) => left.localeCompare(right, "zh-CN"));
  }, [monitor, monitorIsLive, report, status]);

  useEffect(() => {
    if (status !== "ALL" && !statusOptions.includes(status)) {
      setStatus("ALL");
      setOutcome("ALL");
    }
  }, [status, statusOptions]);

  useEffect(() => {
    if (outcome !== "ALL" && !outcomeOptions.includes(outcome)) {
      setOutcome("ALL");
    }
  }, [outcome, outcomeOptions]);

  const rows = useMemo(() => {
    if (!report || !monitorIsLive) return [];
    const normalized = keyword.trim().toLowerCase();
    return report.companies.filter((company) => {
      const result = company.monitor_results[monitor];
      return (
        result !== undefined &&
        (status === "ALL" || result.status === status) &&
        (outcome === "ALL" || result.outcome.trim() === outcome) &&
        (!normalized ||
          company.company_code.toLowerCase().includes(normalized) ||
          company.company_name.toLowerCase().includes(normalized))
      );
    });
  }, [keyword, monitor, monitorIsLive, outcome, report, status]);

  const portfolio = useMemo(() => {
    if (!report) return { alertCompanies: 0, blockedCompanies: 0, alerts: 0 };
    const alertCompanies = report.companies.filter((company) =>
      LIVE_CAPABILITY_CODES.some(
        (code) => company.monitor_results[code]?.status === "ALERT",
      ),
    ).length;
    const blockedCompanies = report.companies.filter((company) =>
      LIVE_CAPABILITY_CODES.some(
        (code) => company.monitor_results[code]?.status === "BLOCKED",
      ),
    ).length;
    const alerts = LIVE_CAPABILITY_CODES.reduce(
      (total, code) => total + (report.monitor_summary[code]?.ALERT ?? 0),
      0,
    );
    return { alertCompanies, blockedCompanies, alerts };
  }, [report]);

  const selectCapability = (code: CapabilityCode) => {
    setMonitor(code);
    setStatus("ALL");
    setOutcome("ALL");
    setKeyword("");
    requestAnimationFrame(() =>
      document
        .getElementById("capability-detail")
        ?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
  };

  const selectStatus = (value: ValidationStatus | "ALL") => {
    setStatus(value);
    setOutcome("ALL");
  };

  const columns: ColumnsType<ValidationCompany> = [
    {
      title: "公司",
      key: "company",
      width: 280,
      fixed: "left",
      render: (_, company) => (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{company.company_code}</Typography.Text>
          <Typography.Text>{company.company_name}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "状态",
      width: 90,
      key: "status",
      render: (_, company) => {
        const result = company.monitor_results[monitor] as MonitorResult;
        const meta = STATUS_META[result.status];
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "检查结论",
      width: 220,
      key: "outcome",
      render: (_, company) => company.monitor_results[monitor]?.outcome ?? "-",
    },
    {
      title: "关键数值",
      width: 390,
      key: "values",
      render: (_, company) => {
        const result = company.monitor_results[monitor];
        if (!result || !report) return "-";
        return (
          <Space direction="vertical" size={2}>
            {(VALUE_ORDER[monitor] ?? []).map((key) => (
              <Typography.Text key={key}>
                {VALUE_LABELS[key]}：
                {formatValue(key, result.values[key] ?? null, report)}
              </Typography.Text>
            ))}
          </Space>
        );
      },
    },
    {
      title: "阻断/限制原因",
      key: "reason",
      width: 360,
      render: (_, company) => company.monitor_results[monitor]?.reason ?? "-",
    },
    {
      title: "候选明细",
      key: "candidates",
      width: 110,
      render: (_, company) => {
        const count = company.monitor_results[monitor]?.candidates?.length ?? 0;
        return count > 0 ? <Tag color="warning">{count} 条</Tag> : "-";
      },
    },
  ];

  const requestSuccessRate = report
    ? report.runtime.request_count === 0
      ? 0
      : ((report.runtime.request_count - report.runtime.request_error_count) /
          report.runtime.request_count) *
        100
    : 0;
  const liveCapabilityCount = LIVE_CAPABILITY_CODES.length;
  const pendingCapabilityCount = CAPABILITIES.length - liveCapabilityCount;

  return (
    <Space
      direction="vertical"
      size="large"
      style={{ width: "100%", maxWidth: 1600, margin: "0 auto" }}
    >
      <Row justify="space-between" align="middle" gutter={[16, 16]}>
        <Col flex="auto">
          <Typography.Title
            level={2}
            style={{
              margin: 0,
              fontSize: 26,
              lineHeight: "36px",
              letterSpacing: 0,
            }}
          >
            集团所得税风险监测驾驶舱
          </Typography.Title>
          <Typography.Text type="secondary">
            六项核心能力的运行结果、数据质量与建设状态
          </Typography.Text>
        </Col>
        <Col flex="none">
          <Button
            onClick={() => void query.refetch()}
            loading={query.isFetching}
          >
            刷新数据
          </Button>
        </Col>
      </Row>

      {query.isPending ? (
        <Alert type="info" showIcon message="正在读取全量监测结果" />
      ) : null}
      {query.isError ? (
        <Alert
          type="error"
          showIcon
          message="全量监测结果尚未生成或读取失败"
          action={
            <Button onClick={() => void query.refetch()}>重新加载</Button>
          }
        />
      ) : null}

      {report ? (
        <>
          <Alert
            type="info"
            showIcon
            message={`${report.fiscal_year}年第${report.quarter}季度 · ${report.company_scope.included_company_count}家公司 · ${liveCapabilityCount}项已运行 / ${pendingCapabilityCount}项待完善`}
            description={`数据生成于 ${new Date(report.generated_at).toLocaleString("zh-CN")}。已运行能力来自 ${report.runtime.request_count} 次真实接口请求；待完善能力仅展示建设状态，不计入正常率。`}
          />

          <Row gutter={[12, 12]} aria-label="整体运行情况">
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="覆盖公司"
                  value={report.company_scope.included_company_count}
                  suffix="家"
                />
              </Card>
            </Col>
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="已运行能力"
                  value={liveCapabilityCount}
                  suffix={`/ ${CAPABILITIES.length}`}
                />
                <Progress
                  percent={Math.round(
                    (liveCapabilityCount / CAPABILITIES.length) * 100,
                  )}
                  showInfo={false}
                  size="small"
                />
              </Card>
            </Col>
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="有示警公司"
                  value={portfolio.alertCompanies}
                  suffix="家"
                  valueStyle={{ color: "#cf1322" }}
                />
                <Typography.Text type="secondary">
                  共 {portfolio.alerts} 个示警项次
                </Typography.Text>
              </Card>
            </Col>
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="数据阻断公司"
                  value={portfolio.blockedCompanies}
                  suffix="家"
                  valueStyle={{ color: "#d46b08" }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="真实请求成功率"
                  value={requestSuccessRate}
                  precision={requestSuccessRate === 100 ? 0 : 2}
                  suffix="%"
                  valueStyle={{ color: "#237804" }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={8} xl={4}>
              <Card size="small" style={{ borderRadius: 6 }}>
                <Statistic
                  title="全量取数耗时"
                  value={report.runtime.external_fetch_seconds}
                  precision={1}
                  suffix="秒"
                />
              </Card>
            </Col>
          </Row>

          <section aria-labelledby="capability-overview-title">
            <Typography.Title
              id="capability-overview-title"
              level={3}
              style={{ margin: "0 0 4px" }}
            >
              六项能力全景
            </Typography.Title>
            <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
              已运行能力展示本期公司结果；待完善能力展示真实数据接入和验收进度。
            </Typography.Paragraph>
            <Row gutter={[12, 12]}>
              {CAPABILITIES.map((capability) => (
                <Col xs={24} md={12} xl={8} key={capability.code}>
                  <CapabilityCard
                    capability={capability}
                    report={report}
                    onSelect={selectCapability}
                  />
                </Col>
              ))}
            </Row>
          </section>

          {selectedCapability.code === "refund" ? (
            <Alert
              type="warning"
              showIcon
              message={report.refund_evidence_notice}
            />
          ) : null}
          {selectedCapability.code === "tax_adjustment_account_accuracy" &&
          report.tax_adjustment_account_accuracy_notice ? (
            <Alert
              type="warning"
              showIcon
              message={report.tax_adjustment_account_accuracy_notice}
            />
          ) : null}

          <section
            id="capability-detail"
            aria-labelledby="capability-detail-title"
          >
            <Typography.Title
              id="capability-detail-title"
              level={3}
              style={{ margin: "0 0 12px" }}
            >
              能力明细
            </Typography.Title>
            <Space
              wrap
              size="middle"
              style={{ marginBottom: 16, width: "100%" }}
            >
              <Select
                aria-label="监测能力"
                value={monitor}
                style={{ width: "min(100%, 430px)" }}
                onChange={(value: CapabilityCode) => selectCapability(value)}
                options={CAPABILITIES.map((capability) => ({
                  value: capability.code,
                  label: capability.name,
                }))}
              />
              {monitorIsLive ? (
                <>
                  <Select
                    aria-label="结果状态"
                    value={status}
                    style={{ width: 130 }}
                    onChange={selectStatus}
                    options={[
                      { value: "ALL", label: "全部状态" },
                      ...statusOptions.map((value) => ({
                        value,
                        label: STATUS_META[value].label,
                      })),
                    ]}
                  />
                  <Select
                    aria-label="检查结论"
                    value={outcome}
                    style={{ width: 220 }}
                    onChange={setOutcome}
                    options={[
                      { value: "ALL", label: "全部结论" },
                      ...outcomeOptions.map((value) => ({
                        value,
                        label: value,
                      })),
                    ]}
                  />
                  <Input.Search
                    aria-label="搜索公司"
                    placeholder="公司代码或名称"
                    allowClear
                    style={{ width: 240 }}
                    value={keyword}
                    onChange={(event) => setKeyword(event.target.value)}
                  />
                  <Typography.Text type="secondary">
                    当前 {rows.length} 家
                  </Typography.Text>
                  <Button
                    type="primary"
                    disabled={rows.length === 0}
                    onClick={() =>
                      downloadValidationDetailsCsv(
                        report,
                        selectedCapability,
                        rows,
                      )
                    }
                  >
                    导出明细
                  </Button>
                </>
              ) : (
                <Tag color="warning">未执行</Tag>
              )}
            </Space>

            {monitorIsLive ? (
              <Table<ValidationCompany>
                rowKey="company_code"
                columns={columns}
                dataSource={rows}
                pagination={{
                  pageSize: 50,
                  showSizeChanger: false,
                  showTotal: (total) => `共 ${total} 家`,
                }}
                expandable={{
                  expandedRowRender: (company) => (
                    <Table<Record<string, string>>
                      rowKey={(candidate) =>
                        candidate.candidate_no ??
                        `${candidate.voucher_no ?? "candidate"}-${candidate.amount ?? "0"}`
                      }
                      columns={CANDIDATE_COLUMNS}
                      dataSource={
                        company.monitor_results[monitor]?.candidates ?? []
                      }
                      pagination={false}
                      scroll={{ x: 1540 }}
                      size="small"
                    />
                  ),
                  rowExpandable: (company) =>
                    (company.monitor_results[monitor]?.candidates?.length ??
                      0) > 0,
                }}
                scroll={{ x: 1450 }}
                size="middle"
              />
            ) : (
              <ReadinessPanel capability={selectedCapability} />
            )}
          </section>
        </>
      ) : null}
    </Space>
  );
}

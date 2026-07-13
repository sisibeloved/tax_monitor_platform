import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Progress, Row, Space, Statistic, Table, Tag, Typography } from "antd";

import {
  operationsSummaryQueryOptions,
  retryOperationsRun,
  type OperationsRun,
} from "./api";


const runStatusLabels: Record<OperationsRun["status"], string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  PARTIAL_SUCCESS: "部分成功",
  SUCCEEDED: "成功",
  FAILED: "失败",
};


export function OperationsDashboard() {
  const query = useQuery(operationsSummaryQueryOptions());
  const retryMutation = useMutation({ mutationFn: retryOperationsRun });
  const summary = query.data;
  const deadlineHours = summary?.t_plus_2_deadline
    ? Math.round(
        (new Date(summary.t_plus_2_deadline).getTime() -
          new Date(summary.generated_at).getTime()) /
          3_600_000,
      )
    : null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {query.isError ? <Alert type="error" message="运维数据加载失败" /> : null}
      {summary?.delivery_status === "AT_RISK" || summary?.delivery_status === "OVERDUE" ? (
        <Alert
          type={summary.delivery_status === "OVERDUE" ? "error" : "warning"}
          showIcon
          message="交付时限预警"
          description={
            deadlineHours === null
              ? "数据尚未达到就绪状态"
              : deadlineHours >= 0
                ? `距 T+2 截止时间约 ${deadlineHours} 小时`
                : `已超过 T+2 截止时间约 ${Math.abs(deadlineHours)} 小时`
          }
        />
      ) : null}

      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Card><Statistic title="数据错误" value={summary?.counters.data_errors ?? 0} valueStyle={{ color: "#d48806" }} /></Card>
        </Col>
        <Col xs={24} md={8}>
          <Card><Statistic title="技术失败" value={summary?.counters.technical_failures ?? 0} valueStyle={{ color: "#cf1322" }} /></Card>
        </Col>
        <Col xs={24} md={8}>
          <Card><Statistic title="税务风险" value={summary?.counters.tax_risks ?? 0} valueStyle={{ color: "#1677ff" }} /></Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card title="关联覆盖率">
            <Progress
              percent={Math.round((summary?.link_coverage_ratio ?? 0) * 100)}
              status={(summary?.link_coverage_ratio ?? 0) >= 0.95 ? "success" : "exception"}
            />
          </Card>
        </Col>
        <Col xs={24} md={6}>
          <Card><Statistic title="证据积压" value={summary?.counters.evidence_backlog ?? 0} /></Card>
        </Col>
        <Col xs={24} md={6}>
          <Card><Statistic title="供应商失败" value={summary?.counters.provider_failures ?? 0} /></Card>
        </Col>
      </Row>

      <Card title="批次与公司执行状态">
        <Typography.Paragraph type="secondary">
          驾驶舱仅展示稳定状态码与汇总数量，不展示敏感自由文本。
        </Typography.Paragraph>
        <Table<OperationsRun>
          rowKey="run_id"
          loading={query.isLoading}
          dataSource={summary?.runs ?? []}
          pagination={false}
          columns={[
            { title: "监测类型", dataIndex: "run_type" },
            { title: "期间", dataIndex: "period" },
            {
              title: "状态",
              dataIndex: "status",
              render: (value: OperationsRun["status"]) => (
                <Tag color={value === "SUCCEEDED" ? "green" : value === "FAILED" ? "red" : "orange"}>
                  {runStatusLabels[value]}
                </Tag>
              ),
            },
            { title: "队列等待（秒）", dataIndex: "queue_wait_seconds" },
            {
              title: "公司结果",
              dataIndex: "company_counts",
              render: (value: OperationsRun["company_counts"]) =>
                `成功 ${value.succeeded} / 数据阻断 ${value.blocked} / 技术失败 ${value.failed}`,
            },
            {
              title: "操作",
              render: (_value, run) =>
                summary?.can_retry && run.company_counts.failed > 0 ? (
                  <Button
                    loading={retryMutation.isPending}
                    onClick={() => retryMutation.mutate(run.run_id)}
                  >
                    重试失败公司
                  </Button>
                ) : null,
            },
          ]}
        />
      </Card>
    </Space>
  );
}

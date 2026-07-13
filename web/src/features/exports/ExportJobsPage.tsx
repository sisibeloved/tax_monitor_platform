import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Space, Table, Tag, Typography } from "antd";

import {
  createBusinessEntertainmentExport,
  exportJobsKey,
  exportJobsQueryOptions,
  issueDownloadUrl,
  type ExportJob,
} from "./api";


const statusLabels: Record<ExportJob["status"], string> = {
  QUEUED: "排队中",
  RUNNING: "生成中",
  COMPLETED: "已完成",
  FAILED: "失败",
  EXPIRED: "已过期",
};


export function ExportJobsPage() {
  const queryClient = useQueryClient();
  const query = useQuery(exportJobsQueryOptions());
  const createMutation = useMutation({
    mutationFn: createBusinessEntertainmentExport,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: exportJobsKey });
    },
  });
  const downloadMutation = useMutation({
    mutationFn: issueDownloadUrl,
    onSuccess: ({ url }) => {
      window.open(url, "_blank", "noopener,noreferrer");
    },
  });

  return (
    <Card
      title="风险清单导出任务"
      extra={
        <Button
          type="primary"
          loading={createMutation.isPending}
          onClick={() => createMutation.mutate()}
        >
          新建业务招待费导出
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          导出范围在创建时冻结，文件下载时会再次校验当前公司权限。
        </Typography.Text>
        {query.isError ? <Alert type="error" message="导出任务加载失败" /> : null}
        <Table<ExportJob>
          rowKey="id"
          loading={query.isLoading}
          dataSource={query.data?.items ?? []}
          pagination={false}
          scroll={{ x: 1_100 }}
          columns={[
            {
              title: "状态",
              dataIndex: "status",
              render: (status: ExportJob["status"]) => (
                <Tag color={status === "COMPLETED" ? "green" : status === "FAILED" ? "red" : "default"}>
                  {statusLabels[status]}
                </Tag>
              ),
            },
            {
              title: "公司范围",
              dataIndex: "company_ids",
              render: (companyIds: string[]) => companyIds.length || "集团范围",
            },
            { title: "行数", dataIndex: "row_count", render: (value: number | null) => value ?? "—" },
            { title: "模式版本", dataIndex: "schema_version" },
            {
              title: "校验和",
              dataIndex: "checksum",
              render: (value: string | null) => value ?? "—",
            },
            {
              title: "过期时间",
              dataIndex: "expires_at",
              render: (value: string) => new Date(value).toLocaleString("zh-CN"),
            },
            {
              title: "失败原因",
              dataIndex: "failure_code",
              render: (value: string | null) => value ?? "—",
            },
            {
              title: "操作",
              render: (_value, record) =>
                record.status === "COMPLETED" && new Date(record.expires_at) > new Date() ? (
                  <Button
                    loading={downloadMutation.isPending}
                    onClick={() => downloadMutation.mutate(record.id)}
                  >
                    安全下载
                  </Button>
                ) : null,
            },
          ]}
        />
      </Space>
    </Card>
  );
}

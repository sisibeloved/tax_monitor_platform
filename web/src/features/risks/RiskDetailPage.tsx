import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  List,
  Modal,
  Radio,
  Space,
  Tag,
  Typography,
} from "antd";
import { useEffect, useState } from "react";

import {
  businessEntertainmentCaseKey,
  businessEntertainmentRiskListKey,
  resolveCaseToSap,
  riskDetailQueryOptions,
} from "./api";

interface RiskDetailPageProps {
  caseId: string | null;
  onClose: () => void;
}

export function RiskDetailPage({ caseId, onClose }: RiskDetailPageProps) {
  const queryClient = useQueryClient();
  const detail = useQuery(riskDetailQueryOptions(caseId));
  const [resolveOpen, setResolveOpen] = useState(false);
  const [evidenceLinkId, setEvidenceLinkId] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: async () => {
      if (caseId === null || detail.data === undefined || evidenceLinkId === null) {
        throw new Error("请选择精确关联证据");
      }
      return resolveCaseToSap(caseId, evidenceLinkId, detail.data.row_version);
    },
    onSuccess: async () => {
      setResolveOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: businessEntertainmentRiskListKey }),
        queryClient.invalidateQueries({ queryKey: businessEntertainmentCaseKey(caseId) }),
        queryClient.invalidateQueries({ queryKey: ["quarterly-dashboard"] }),
        queryClient.invalidateQueries({ queryKey: ["business-entertainment-coverage"] }),
        queryClient.invalidateQueries({ queryKey: ["business-entertainment-kpi"] }),
        queryClient.invalidateQueries({ queryKey: ["business-entertainment-export"] }),
      ]);
    },
  });

  useEffect(() => {
    const first = detail.data?.resolution_evidence_links[0];
    setEvidenceLinkId(first?.evidence_link_id ?? null);
  }, [detail.data]);

  const data = detail.data;
  return (
    <Drawer
      title="业务招待费风险详情"
      width={720}
      open={caseId !== null}
      onClose={onClose}
      destroyOnClose
    >
      {detail.isPending ? <Alert type="info" message="正在加载风险详情" /> : null}
      {detail.isError ? <Alert type="error" message="风险详情加载失败" /> : null}
      {data ? (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label="公司">
              {data.company_code} {data.company_name}
            </Descriptions.Item>
            <Descriptions.Item label="状态">{data.status}</Descriptions.Item>
            <Descriptions.Item label="来源模式">{data.source_mode}</Descriptions.Item>
            <Descriptions.Item label="SAP关联状态">
              {data.sap_link_status === "PENDING_LOCATION"
                ? "待定位SAP凭证"
                : "已关联SAP凭证"}
            </Descriptions.Item>
            <Descriptions.Item label="风险金额">
              {data.risk_amount} {data.currency}
            </Descriptions.Item>
            <Descriptions.Item label="金额来源">
              {data.risk_amount_source}
            </Descriptions.Item>
            <Descriptions.Item label="判断标签">
              {data.semantic_label}
            </Descriptions.Item>
            <Descriptions.Item label="置信度">
              {data.confidence_tier}
            </Descriptions.Item>
          </Descriptions>

          <section aria-label="证据引用">
            <Typography.Title level={5}>证据引用</Typography.Title>
            <List
              dataSource={data.evidence_refs}
              locale={{ emptyText: "暂无证据引用" }}
              renderItem={(item) => (
                <List.Item>
                  <Typography.Text strong>{item.field_name}：</Typography.Text>
                  <Typography.Text>{item.quoted_text}</Typography.Text>
                </List.Item>
              )}
            />
          </section>

          <section aria-label="改账建议">
            <Typography.Title level={5}>改账建议</Typography.Title>
            <Space wrap>
              {data.recommended_account_ids.map((account) => (
                <Tag color="blue" key={account}>
                  {account}
                </Tag>
              ))}
            </Space>
            <Typography.Paragraph>{data.rationale_summary}</Typography.Paragraph>
          </section>

          <Divider />
          <Alert
            type="warning"
            showIcon
            message="仅允许使用已持久化的精确关联"
            description="金额、日期或人员相似的模糊关系不能用于解决风险事项。"
          />
          {data.sap_link_status === "PENDING_LOCATION" ? (
            <Button
              type="primary"
              disabled={data.resolution_evidence_links.length === 0}
              onClick={() => setResolveOpen(true)}
            >
              关联SAP凭证
            </Button>
          ) : null}
          {data.resolution_evidence_links.length === 0 &&
          data.sap_link_status === "PENDING_LOCATION" ? (
            <Empty description="尚无可用的精确关联证据" />
          ) : null}
        </Space>
      ) : null}
      <Modal
        title="确认精确关联并解决风险"
        open={resolveOpen}
        okText="确认解决"
        cancelText="取消"
        confirmLoading={mutation.isPending}
        okButtonProps={{ disabled: evidenceLinkId === null }}
        onCancel={() => setResolveOpen(false)}
        onOk={() => mutation.mutate()}
      >
        <Radio.Group
          value={evidenceLinkId}
          onChange={(event) => setEvidenceLinkId(String(event.target.value))}
        >
          <Space direction="vertical">
            {data?.resolution_evidence_links.map((link) => (
              <Radio key={link.evidence_link_id} value={link.evidence_link_id}>
                {link.sap_document_number} / {link.sap_line_item}（精确关联）
              </Radio>
            ))}
          </Space>
        </Radio.Group>
        {mutation.isError ? (
          <Alert type="error" message="关联失败，请刷新后重试" />
        ) : null}
      </Modal>
    </Drawer>
  );
}

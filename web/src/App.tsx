import { Layout, Tabs, Typography } from "antd";

import { SapLinkCoveragePage } from "./features/business-entertainment/SapLinkCoveragePage";
import { ExportJobsPage } from "./features/exports/ExportJobsPage";
import { FullValidationPage } from "./features/full-validation/FullValidationPage";
import { IncomeTaxRefundPage } from "./features/income-tax-refunds/IncomeTaxRefundPage";
import { OperationsDashboard } from "./features/operations/OperationsDashboard";
import { QuarterlyDashboardPage } from "./features/quarterly/QuarterlyDashboardPage";
import { RiskListPage } from "./features/risks/RiskListPage";

const { Content, Header } = Layout;

export default function App() {
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center" }}>
        <Typography.Title
          level={1}
          style={{
            color: "white",
            margin: 0,
            fontSize: 24,
            lineHeight: "32px",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          集团所得税风险监测
        </Typography.Title>
      </Header>
      <Content style={{ padding: 24 }}>
        <Tabs
          defaultActiveKey="full-validation"
          items={[
            {
              key: "full-validation",
              label: "六项监测驾驶舱",
              children: <FullValidationPage />,
            },
            {
              key: "quarterly",
              label: "季度所得税监测",
              children: <QuarterlyDashboardPage />,
            },
            {
              key: "income-tax-refunds",
              label: "所得税退税进度监控及入账科目准确性检查",
              children: <IncomeTaxRefundPage />,
            },
            {
              key: "entertainment-risks",
              label: "业务招待费风险",
              children: <RiskListPage />,
            },
            {
              key: "sap-coverage",
              label: "SAP关联覆盖",
              children: <SapLinkCoveragePage />,
            },
            {
              key: "operations",
              label: "运维驾驶舱",
              children: <OperationsDashboard />,
            },
            {
              key: "exports",
              label: "安全导出",
              children: <ExportJobsPage />,
            },
          ]}
        />
      </Content>
    </Layout>
  );
}

import { Layout, Typography } from "antd";

import { QuarterlyDashboardPage } from "./features/quarterly/QuarterlyDashboardPage";

const { Content, Header } = Layout;

export default function App() {
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center" }}>
        <Typography.Title level={1} style={{ color: "white", margin: 0 }}>
          集团所得税风险监测
        </Typography.Title>
      </Header>
      <Content style={{ padding: 24 }}>
        <QuarterlyDashboardPage />
      </Content>
    </Layout>
  );
}

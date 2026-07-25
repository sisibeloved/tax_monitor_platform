import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Avatar,
  Button,
  Dropdown,
  Layout,
  Spin,
  Tabs,
  Typography,
  message,
} from "antd";
import { useState } from "react";

import "./App.css";
import {
  type AuthSession,
  authSessionQueryKey,
  getAuthSession,
  logout,
} from "./features/auth/api";
import { LoginPage } from "./features/auth/LoginPage";
import { SapLinkCoveragePage } from "./features/business-entertainment/SapLinkCoveragePage";
import { ExportJobsPage } from "./features/exports/ExportJobsPage";
import { FullValidationPage } from "./features/full-validation/FullValidationPage";
import { IncomeTaxRefundPage } from "./features/income-tax-refunds/IncomeTaxRefundPage";
import { OperationsDashboard } from "./features/operations/OperationsDashboard";
import { QuarterlyDashboardPage } from "./features/quarterly/QuarterlyDashboardPage";
import { RiskListPage } from "./features/risks/RiskListPage";

const { Content, Header } = Layout;

export default function App() {
  const queryClient = useQueryClient();
  const [signedOut, setSignedOut] = useState(false);
  const sessionQuery = useQuery({
    queryKey: authSessionQueryKey,
    queryFn: getAuthSession,
    retry: false,
    staleTime: 60 * 1000,
  });

  const acceptSession = (session: AuthSession) => {
    setSignedOut(false);
    queryClient.setQueryData(authSessionQueryKey, session);
  };

  if (sessionQuery.isPending && !signedOut) {
    return (
      <main className="auth-loading">
        <div className="brand-symbol" aria-hidden="true">
          税
        </div>
        <Typography.Title level={1}>集团所得税风险监测平台</Typography.Title>
        <Spin size="small" />
      </main>
    );
  }

  if (signedOut || sessionQuery.data === undefined) {
    return (
      <LoginPage
        onAuthenticated={acceptSession}
        sessionUnavailable={
          sessionQuery.isError && !isUnauthorized(sessionQuery.error)
        }
      />
    );
  }

  const finishLogout = async () => {
    try {
      await logout();
      setSignedOut(true);
      queryClient.removeQueries({ queryKey: authSessionQueryKey });
    } catch {
      message.error("退出失败，请稍后重试。");
    }
  };

  return (
    <AuthenticatedApp session={sessionQuery.data} onLogout={finishLogout} />
  );
}

function AuthenticatedApp({
  session,
  onLogout,
}: {
  session: AuthSession;
  onLogout: () => Promise<void>;
}) {
  const [loggingOut, setLoggingOut] = useState(false);
  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await onLogout();
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <Layout className="app-layout">
      <Header className="app-header">
        <div className="app-brand">
          <span className="app-brand-symbol" aria-hidden="true">
            税
          </span>
          <Typography.Title level={1}>集团所得税风险监测</Typography.Title>
        </div>
        <Dropdown
          menu={{
            items: [{ key: "logout", label: "退出登录", danger: true }],
            onClick: () => void handleLogout(),
          }}
          placement="bottomRight"
          trigger={["click"]}
        >
          <Button
            className="user-menu-trigger"
            loading={loggingOut}
            type="text"
          >
            <Avatar size={30} src={session.avatar_url ?? undefined}>
              {session.display_name.slice(0, 1)}
            </Avatar>
            <span className="user-display-name">{session.display_name}</span>
            <span className="menu-chevron" aria-hidden="true">
              ⌄
            </span>
          </Button>
        </Dropdown>
      </Header>
      <Content className="app-content">
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
            { key: "exports", label: "安全导出", children: <ExportJobsPage /> },
          ]}
        />
      </Content>
    </Layout>
  );
}

function isUnauthorized(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    error.status === 401
  );
}

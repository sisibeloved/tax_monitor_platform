import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Divider, Form, Input, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";

import { ApiError } from "../../api/client";
import {
  type AuthSession,
  feishuLoginUrl,
  getAuthConfiguration,
  loginWithPassword,
} from "./api";

interface LoginPageProps {
  onAuthenticated: (session: AuthSession) => void;
  sessionUnavailable?: boolean;
}

interface LoginFormValues {
  username: string;
  password: string;
}

const oauthErrors: Readonly<Record<string, string>> = {
  invalid_state: "登录请求已失效，请重新发起飞书登录。",
  access_denied: "飞书授权未完成，请重试。",
  not_authorized: "当前飞书账号尚未获得平台访问权限。",
  provider_unavailable: "暂时无法连接飞书，请稍后重试。",
};

export function LoginPage({
  onAuthenticated,
  sessionUnavailable,
}: LoginPageProps) {
  const [submitting, setSubmitting] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const configuration = useQuery({
    queryKey: ["authentication", "configuration"],
    queryFn: getAuthConfiguration,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const oauthError = useMemo(() => {
    const code = new URLSearchParams(window.location.search).get("auth_error");
    return code === null
      ? null
      : (oauthErrors[code] ?? "授权登录未完成，请重试。");
  }, []);

  useEffect(() => {
    if (oauthError === null) {
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("auth_error");
    window.history.replaceState(
      {},
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, [oauthError]);

  const passwordEnabled = configuration.data?.password_enabled ?? true;
  const feishuEnabled = configuration.data?.feishu_enabled ?? true;
  const noLoginMethod =
    configuration.isSuccess &&
    !configuration.data.password_enabled &&
    !configuration.data.feishu_enabled;

  const submit = async (values: LoginFormValues) => {
    setSubmitting(true);
    setLoginError(null);
    try {
      onAuthenticated(await loginWithPassword(values));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setLoginError("账号或密码不正确，请重新输入。");
      } else if (error instanceof ApiError && error.status === 429) {
        setLoginError("尝试次数过多，请稍后再试。");
      } else {
        setLoginError("登录服务暂时不可用，请稍后重试。");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-shell">
      <section className="login-brand" aria-label="集团所得税风险监测平台">
        <div className="brand-lockup">
          <span className="brand-symbol" aria-hidden="true">
            税
          </span>
          <div>
            <div className="brand-name">集团所得税风险监测平台</div>
            <div className="brand-en">TAX RISK MONITORING</div>
          </div>
        </div>

        <div className="brand-statement">
          <Typography.Text>企业税务风控工作台</Typography.Text>
          <Typography.Title level={1}>
            风险监测
            <br />
            清晰、及时、可追溯
          </Typography.Title>
        </div>

        <div className="risk-visual" aria-hidden="true">
          <div className="visual-axis">
            <span>风险趋势</span>
            <strong>2026 · Q3</strong>
          </div>
          <div className="visual-bars">
            {[36, 58, 47, 72, 55, 83, 64, 42, 68, 51, 76, 61].map(
              (height, index) => (
                <span
                  className={index === 5 || index === 10 ? "is-alert" : ""}
                  key={`${height}-${index}`}
                  style={{ height: `${height}%` }}
                />
              ),
            )}
          </div>
          <div className="visual-footer">
            <span>
              <i className="status-dot status-dot--normal" />
              持续监测
            </span>
            <span>
              <i className="status-dot status-dot--alert" />
              风险响应
            </span>
          </div>
        </div>
      </section>

      <section className="login-access">
        <div className="login-form-wrap">
          <header className="login-heading">
            <span className="login-kicker">SECURE ACCESS</span>
            <Typography.Title level={2}>欢迎回来</Typography.Title>
            <Typography.Text type="secondary">
              登录税务风险监测平台
            </Typography.Text>
          </header>

          {(oauthError ?? loginError) && (
            <Alert
              className="login-alert"
              message={oauthError ?? loginError}
              type="error"
              showIcon
            />
          )}
          {(sessionUnavailable || configuration.isError) &&
            !loginError &&
            !oauthError && (
              <Alert
                className="login-alert"
                message="认证服务暂时不可用，请稍后重试。"
                type="warning"
                showIcon
              />
            )}
          {noLoginMethod && (
            <Alert
              className="login-alert"
              message="系统尚未配置登录方式，请联系管理员。"
              type="info"
              showIcon
            />
          )}

          {passwordEnabled && (
            <Form<LoginFormValues>
              layout="vertical"
              requiredMark={false}
              onFinish={submit}
              className="login-form"
            >
              <Form.Item
                label="账号"
                name="username"
                rules={[{ required: true, message: "请输入账号" }]}
              >
                <Input
                  autoComplete="username"
                  autoFocus
                  disabled={submitting}
                  maxLength={128}
                  placeholder="请输入账号"
                  size="large"
                />
              </Form.Item>
              <Form.Item
                label="密码"
                name="password"
                rules={[{ required: true, message: "请输入密码" }]}
              >
                <Input.Password
                  autoComplete="current-password"
                  disabled={submitting}
                  maxLength={1024}
                  placeholder="请输入密码"
                  size="large"
                />
              </Form.Item>
              <Button
                block
                className="login-submit"
                htmlType="submit"
                loading={submitting}
                size="large"
                type="primary"
              >
                登录
              </Button>
            </Form>
          )}

          {passwordEnabled && feishuEnabled && <Divider plain>或</Divider>}

          {feishuEnabled && (
            <Button
              block
              className="feishu-login"
              disabled={submitting}
              onClick={() => window.location.assign(feishuLoginUrl("/"))}
              size="large"
            >
              <span className="feishu-mark" aria-hidden="true">
                飞
              </span>
              使用飞书授权登录
            </Button>
          )}
        </div>

        <footer className="login-footer">
          HAILIANG EDUCATION · TAX CONTROL
        </footer>
      </section>
    </main>
  );
}

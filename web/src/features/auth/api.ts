import { apiGet, apiPost, apiUrl } from "../../api/client";

export interface AuthConfiguration {
  password_enabled: boolean;
  feishu_enabled: boolean;
}

export interface AuthSession {
  authenticated: true;
  subject: string;
  display_name: string;
  avatar_url: string | null;
  auth_method: string;
  roles: string[];
  organization_path: string;
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export const authSessionQueryKey = ["authentication", "session"] as const;

export function getAuthConfiguration(): Promise<AuthConfiguration> {
  return apiGet<AuthConfiguration>("/api/v1/auth/config");
}

export function getAuthSession(): Promise<AuthSession> {
  return apiGet<AuthSession>("/api/v1/auth/session");
}

export function loginWithPassword(
  credentials: LoginCredentials,
): Promise<AuthSession> {
  return apiPost<AuthSession, LoginCredentials>(
    "/api/v1/auth/login",
    credentials,
  );
}

export function logout(): Promise<{ authenticated: false }> {
  return apiPost<{ authenticated: false }, Record<string, never>>(
    "/api/v1/auth/logout",
    {},
  );
}

export function feishuLoginUrl(returnTo = "/"): string {
  const query = new URLSearchParams({ return_to: returnTo });
  return apiUrl(`/api/v1/auth/feishu/start?${query.toString()}`);
}

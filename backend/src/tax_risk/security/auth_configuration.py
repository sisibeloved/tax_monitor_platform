"""Build browser authentication services from validated settings."""

from tax_risk.adapters.lark.oauth import FeishuOAuthClient, FeishuOAuthConfiguration
from tax_risk.config import Settings
from tax_risk.security.authentication import AuthenticationService


def build_authentication_service(settings: Settings) -> AuthenticationService:
    session_secret = (
        settings.auth_session_secret.get_secret_value()
        if settings.auth_session_secret is not None
        else None
    )
    return AuthenticationService(
        session_secret=session_secret,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        oauth_state_ttl_seconds=settings.auth_oauth_state_ttl_seconds,
        local_accounts=settings.auth_local_accounts,
        feishu_principals=settings.auth_feishu_principals,
        feishu_tenant_key=settings.auth_feishu_tenant_key,
        login_max_failures=settings.auth_login_max_failures,
        login_window_seconds=settings.auth_login_window_seconds,
    )


def build_feishu_oauth_client(settings: Settings) -> FeishuOAuthClient | None:
    if not settings.auth_feishu_enabled:
        return None
    assert settings.auth_feishu_client_id is not None
    assert settings.auth_feishu_client_secret is not None
    assert settings.auth_feishu_redirect_uri is not None
    return FeishuOAuthClient(
        FeishuOAuthConfiguration(
            client_id=settings.auth_feishu_client_id.strip(),
            client_secret=settings.auth_feishu_client_secret.get_secret_value(),
            redirect_uri=settings.auth_feishu_redirect_uri.strip(),
            timeout_seconds=settings.auth_feishu_timeout_seconds,
        )
    )


__all__ = ["build_authentication_service", "build_feishu_oauth_client"]

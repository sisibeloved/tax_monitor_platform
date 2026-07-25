from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tax_risk.api.routes.auth import router
from tax_risk.config import Settings
from tax_risk.security.auth_configuration import build_authentication_service
from tax_risk.security.authentication import FeishuIdentity, hash_password


def _mapping(*, password_hash: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "subject": "tax-user",
        "display_name": "税务用户",
        "roles": ["group-tax"],
        "allowed_company_ids": [],
        "organization_path": "/group/tax",
    }
    if password_hash is not None:
        result["password_hash"] = password_hash
    return result


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "auth_session_secret": "test-session-secret-at-least-32-characters",
        "auth_local_accounts": {
            "tax.user": _mapping(password_hash=hash_password("correct-password"))
        },
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _app(settings: Settings, feishu_provider: object | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.principal_provider = None
    app.state.authentication_service = build_authentication_service(settings)
    app.state.feishu_oauth_client = feishu_provider
    app.include_router(router)
    return app


def test_password_login_session_and_logout() -> None:
    with TestClient(_app(_settings())) as client:
        configuration = client.get("/api/v1/auth/config")
        rejected = client.post(
            "/api/v1/auth/login",
            json={"username": "tax.user", "password": "wrong"},
        )
        accepted = client.post(
            "/api/v1/auth/login",
            json={"username": "tax.user", "password": "correct-password"},
        )
        session = client.get("/api/v1/auth/session")
        logout = client.post("/api/v1/auth/logout", json={})
        after_logout = client.get("/api/v1/auth/session")

    assert configuration.json() == {"password_enabled": True, "feishu_enabled": False}
    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["display_name"] == "税务用户"
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert session.status_code == 200
    assert logout.json() == {"authenticated": False}
    assert after_logout.status_code == 401


class _FakeFeishuProvider:
    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://accounts.example/authorize?state={state}&challenge={code_challenge}"

    async def exchange_identity(self, *, code: str, code_verifier: str) -> FeishuIdentity:
        assert code == "authorization-code"
        assert len(code_verifier) >= 43
        return FeishuIdentity(
            open_id="ou_authorized",
            tenant_key="tenant-authorized",
            name="飞书授权用户",
        )


def test_feishu_oauth_start_and_callback_create_a_session() -> None:
    settings = _settings(
        auth_feishu_enabled=True,
        auth_feishu_client_id="cli_test",
        auth_feishu_client_secret="client-secret",
        auth_feishu_redirect_uri="https://app.example/api/v1/auth/feishu/callback",
        auth_feishu_tenant_key="tenant-authorized",
        auth_feishu_principals={"ou_authorized": _mapping()},
    )
    with TestClient(_app(settings, _FakeFeishuProvider())) as client:
        start = client.get(
            "/api/v1/auth/feishu/start?return_to=/dashboard",
            follow_redirects=False,
        )
        query = parse_qs(urlsplit(start.headers["location"]).query)
        callback = client.get(
            "/api/v1/auth/feishu/callback",
            params={"state": query["state"][0], "code": "authorization-code"},
            follow_redirects=False,
        )
        session = client.get("/api/v1/auth/session")

    assert start.status_code == 302
    assert "challenge" in query
    assert callback.status_code == 302
    assert callback.headers["location"] == "/dashboard"
    assert session.json()["auth_method"] == "feishu"
    assert session.json()["display_name"] == "税务用户"

"""Browser authentication endpoints for password and Feishu OAuth login."""

from __future__ import annotations

from typing import Annotated, Protocol, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from tax_risk.adapters.lark.oauth import FeishuOAuthError
from tax_risk.api.dependencies import get_principal
from tax_risk.config import Settings
from tax_risk.security.authentication import (
    AuthenticatedIdentity,
    AuthenticationError,
    AuthenticationService,
    FeishuIdentity,
    LoginRateLimited,
)
from tax_risk.security.principal import Principal

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class FeishuOAuthProvider(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...

    async def exchange_identity(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> FeishuIdentity: ...


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class AuthConfigurationResponse(BaseModel):
    password_enabled: bool
    feishu_enabled: bool


class SessionResponse(BaseModel):
    authenticated: bool = True
    subject: str
    display_name: str
    avatar_url: str | None
    auth_method: str
    roles: list[str]
    organization_path: str


class LogoutResponse(BaseModel):
    authenticated: bool = False


@router.get("/config", response_model=AuthConfigurationResponse)
def get_auth_configuration(request: Request) -> AuthConfigurationResponse:
    service = _auth_service(request)
    return AuthConfigurationResponse(
        password_enabled=service.password_enabled,
        feishu_enabled=_feishu_enabled(request),
    )


@router.post("/login", response_model=SessionResponse)
def login(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
    service = _auth_service(request)
    if not service.password_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password login is not configured",
        )
    attempt_key = request.client.host if request.client is not None else "unknown"
    try:
        identity = service.authenticate_password(
            body.username,
            body.password,
            attempt_key=attempt_key,
        )
    except LoginRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(_settings(request).auth_login_window_seconds)},
        ) from error
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        ) from error
    _set_session_cookie(response, request, service.issue_session(identity))
    request.state.principal = identity.principal
    request.state.auth_identity = identity
    return _session_response(identity)


@router.get("/session", response_model=SessionResponse)
def get_session(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> SessionResponse:
    identity = getattr(request.state, "auth_identity", None)
    if not isinstance(identity, AuthenticatedIdentity):
        identity = AuthenticatedIdentity(
            principal=principal,
            display_name=principal.subject,
            avatar_url=None,
            auth_method="upstream",
        )
    return _session_response(identity)


@router.post("/logout", response_model=LogoutResponse)
def logout(request: Request, response: Response) -> LogoutResponse:
    settings = _settings(request)
    response.delete_cookie(
        settings.auth_session_cookie_name,
        path="/",
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse()


@router.get("/feishu/start")
def start_feishu_login(
    request: Request,
    return_to: Annotated[str, Query(max_length=2048)] = "/",
) -> RedirectResponse:
    if not _feishu_enabled(request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feishu login is not configured",
        )
    service = _auth_service(request)
    transaction = service.issue_oauth_transaction(return_to=return_to)
    provider = _feishu_provider(request)
    response = RedirectResponse(
        provider.authorization_url(
            state=transaction.state,
            code_challenge=transaction.code_challenge,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    _set_oauth_cookie(response, request, transaction.cookie_value)
    return response


@router.get("/feishu/callback")
async def finish_feishu_login(
    request: Request,
    state_value: Annotated[str | None, Query(alias="state", max_length=512)] = None,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    settings = _settings(request)
    actual_oauth_cookie = request.cookies.get(_oauth_cookie_name(settings))
    try:
        verifier, return_to = _auth_service(request).consume_oauth_transaction(
            actual_oauth_cookie,
            state_value,
        )
    except AuthenticationError:
        return _oauth_failure(request, "invalid_state")
    if error is not None or code is None:
        return _oauth_failure(request, "access_denied")
    try:
        feishu_user = await _feishu_provider(request).exchange_identity(
            code=code,
            code_verifier=verifier,
        )
        identity = _auth_service(request).resolve_feishu_identity(feishu_user)
    except AuthenticationError:
        return _oauth_failure(request, "not_authorized")
    except FeishuOAuthError:
        return _oauth_failure(request, "provider_unavailable")
    response = RedirectResponse(return_to, status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, request, _auth_service(request).issue_session(identity))
    _delete_oauth_cookie(response, request)
    request.state.principal = identity.principal
    request.state.auth_identity = identity
    return response


def _session_response(identity: AuthenticatedIdentity) -> SessionResponse:
    principal = identity.principal
    return SessionResponse(
        subject=principal.subject,
        display_name=identity.display_name,
        avatar_url=identity.avatar_url,
        auth_method=identity.auth_method,
        roles=sorted(principal.roles),
        organization_path=principal.organization_path,
    )


def _set_session_cookie(response: Response, request: Request, value: str) -> None:
    settings = _settings(request)
    response.set_cookie(
        settings.auth_session_cookie_name,
        value,
        max_age=settings.auth_session_ttl_seconds,
        path="/",
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )


def _set_oauth_cookie(response: Response, request: Request, value: str) -> None:
    settings = _settings(request)
    response.set_cookie(
        _oauth_cookie_name(settings),
        value,
        max_age=settings.auth_oauth_state_ttl_seconds,
        path="/api/v1/auth/feishu",
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )


def _delete_oauth_cookie(response: Response, request: Request) -> None:
    settings = _settings(request)
    response.delete_cookie(
        _oauth_cookie_name(settings),
        path="/api/v1/auth/feishu",
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )


def _oauth_failure(request: Request, code: str) -> RedirectResponse:
    response = RedirectResponse(
        f"/?{urlencode({'auth_error': code})}",
        status_code=status.HTTP_302_FOUND,
    )
    _delete_oauth_cookie(response, request)
    return response


def _oauth_cookie_name(settings: Settings) -> str:
    return f"{settings.auth_session_cookie_name}_oauth"


def _feishu_enabled(request: Request) -> bool:
    return bool(
        _settings(request).auth_feishu_enabled
        and _auth_service(request).feishu_mapping_enabled
        and getattr(request.app.state, "feishu_oauth_client", None) is not None
    )


def _auth_service(request: Request) -> AuthenticationService:
    return cast(AuthenticationService, request.app.state.authentication_service)


def _feishu_provider(request: Request) -> FeishuOAuthProvider:
    provider = cast(
        FeishuOAuthProvider | None,
        getattr(request.app.state, "feishu_oauth_client", None),
    )
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feishu login is not configured",
        )
    return provider


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


__all__ = ["FeishuOAuthProvider", "router"]

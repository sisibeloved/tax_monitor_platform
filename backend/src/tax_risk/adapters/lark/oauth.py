"""Feishu OAuth 2.0 authorization-code adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from tax_risk.security.authentication import FeishuIdentity


class FeishuOAuthError(RuntimeError):
    """The Feishu authorization server rejected or could not complete a request."""


@dataclass(frozen=True, slots=True)
class FeishuOAuthConfiguration:
    client_id: str
    client_secret: str
    redirect_uri: str
    authorize_url: str = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    token_url: str = "https://accounts.feishu.cn/oauth/v3/token"
    user_info_url: str = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    timeout_seconds: float = 15


class FeishuOAuthClient:
    def __init__(self, configuration: FeishuOAuthConfiguration) -> None:
        self._configuration = configuration

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._configuration.client_id,
                "response_type": "code",
                "redirect_uri": self._configuration.redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._configuration.authorize_url}?{query}"

    async def exchange_identity(self, *, code: str, code_verifier: str) -> FeishuIdentity:
        if not code.strip() or len(code) > 4096:
            raise FeishuOAuthError("Feishu authorization code is invalid")
        timeout = httpx.Timeout(self._configuration.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            token_response = await self._post_token(client, code, code_verifier)
            access_token = token_response.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise FeishuOAuthError("Feishu token response omitted access_token")
            return await self._get_user_info(client, access_token)

    async def _post_token(
        self,
        client: httpx.AsyncClient,
        code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        try:
            response = await client.post(
                self._configuration.token_url,
                headers={"Accept": "application/json"},
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._configuration.client_id,
                    "client_secret": self._configuration.client_secret,
                    "code": code,
                    "redirect_uri": self._configuration.redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
            payload = _json_object(response)
        except httpx.HTTPError as error:
            raise FeishuOAuthError("Feishu token request failed") from error
        if response.status_code >= 400 or payload.get("code") not in {None, 0}:
            raise FeishuOAuthError("Feishu rejected the authorization code")
        return payload

    async def _get_user_info(
        self,
        client: httpx.AsyncClient,
        access_token: str,
    ) -> FeishuIdentity:
        try:
            response = await client.get(
                self._configuration.user_info_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            payload = _json_object(response)
        except httpx.HTTPError as error:
            raise FeishuOAuthError("Feishu user-info request failed") from error
        data = payload.get("data")
        if response.status_code >= 400 or payload.get("code") != 0 or not isinstance(data, dict):
            raise FeishuOAuthError("Feishu user-info response was rejected")
        open_id = data.get("open_id")
        tenant_key = data.get("tenant_key")
        name = data.get("name")
        avatar_url = data.get("avatar_url")
        if (
            not isinstance(open_id, str)
            or not open_id.strip()
            or not isinstance(tenant_key, str)
            or not tenant_key.strip()
            or not isinstance(name, str)
            or not name.strip()
            or (avatar_url is not None and not isinstance(avatar_url, str))
        ):
            raise FeishuOAuthError("Feishu user-info response omitted identity fields")
        return FeishuIdentity(
            open_id=open_id.strip(),
            tenant_key=tenant_key.strip(),
            name=name.strip(),
            avatar_url=avatar_url,
        )


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload: Any = response.json()
    except ValueError as error:
        raise FeishuOAuthError("Feishu returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise FeishuOAuthError("Feishu response must be a JSON object")
    return cast(dict[str, object], payload)


__all__ = [
    "FeishuOAuthClient",
    "FeishuOAuthConfiguration",
    "FeishuOAuthError",
]

"""Local credentials, signed browser sessions, and OAuth transaction state."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from hashlib import scrypt, sha256
from threading import Lock
from typing import cast
from uuid import UUID

from tax_risk.security.principal import API_ROLES, Principal

_PASSWORD_SCHEME = "scrypt"
_PASSWORD_N = 2**14
_PASSWORD_R = 8
_PASSWORD_P = 1
_PASSWORD_MAX_LENGTH = 1024
_COOKIE_PAYLOAD_VERSION = 1
_COOKIE_SIGNATURE_BYTES = 32
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._@-]{1,128}")


class AuthenticationError(ValueError):
    """Authentication input or signed state is invalid."""


class LoginRateLimited(AuthenticationError):
    """The caller exceeded the bounded login-attempt window."""


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    principal: Principal
    display_name: str
    avatar_url: str | None
    auth_method: str


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    state: str
    cookie_value: str
    code_challenge: str


@dataclass(frozen=True, slots=True)
class FeishuIdentity:
    open_id: str
    tenant_key: str
    name: str
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class _ConfiguredAccount:
    username: str
    password_hash: str
    identity: AuthenticatedIdentity


@dataclass(frozen=True, slots=True)
class _AttemptWindow:
    failures: tuple[float, ...]


class LoginAttemptLimiter:
    """Small process-local guard complementing gateway-level rate limits."""

    def __init__(self, *, max_failures: int, window_seconds: int) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._windows: dict[str, _AttemptWindow] = {}
        self._lock = Lock()

    def check(self, key: str, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            failures = self._active_failures(key, timestamp)
            if len(failures) >= self._max_failures:
                raise LoginRateLimited("too many login attempts")

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            failures = self._active_failures(key, timestamp)
            self._windows[key] = _AttemptWindow((*failures, timestamp))

    def clear(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)

    def _active_failures(self, key: str, now: float) -> tuple[float, ...]:
        threshold = now - self._window_seconds
        window = self._windows.get(key)
        failures = tuple(value for value in (window.failures if window else ()) if value >= threshold)
        if failures:
            self._windows[key] = _AttemptWindow(failures)
        else:
            self._windows.pop(key, None)
        return failures


class AuthenticationService:
    """Authenticate configured users and issue server-signed browser state."""

    def __init__(
        self,
        *,
        session_secret: str | None,
        session_ttl_seconds: int,
        oauth_state_ttl_seconds: int,
        local_accounts: Mapping[str, Mapping[str, object]],
        feishu_principals: Mapping[str, Mapping[str, object]],
        feishu_tenant_key: str | None,
        login_max_failures: int,
        login_window_seconds: int,
    ) -> None:
        self._secret = session_secret.encode("utf-8") if session_secret else None
        self._session_ttl_seconds = session_ttl_seconds
        self._oauth_state_ttl_seconds = oauth_state_ttl_seconds
        self._local_accounts = _parse_local_accounts(local_accounts)
        self._feishu_principals = {
            open_id.strip(): _identity_from_mapping(
                mapping,
                default_subject=f"feishu:{open_id.strip()}",
                default_display_name=open_id.strip(),
                auth_method="feishu",
            )
            for open_id, mapping in feishu_principals.items()
            if open_id.strip()
        }
        self._feishu_tenant_key = feishu_tenant_key.strip() if feishu_tenant_key else None
        self._limiter = LoginAttemptLimiter(
            max_failures=login_max_failures,
            window_seconds=login_window_seconds,
        )
        if (self._local_accounts or self._feishu_principals) and self._secret is None:
            raise ValueError("configured authentication requires a session secret")

    @property
    def password_enabled(self) -> bool:
        return bool(self._local_accounts)

    @property
    def feishu_mapping_enabled(self) -> bool:
        return bool(self._feishu_principals and self._feishu_tenant_key)

    def authenticate_password(
        self,
        username: str,
        password: str,
        *,
        attempt_key: str,
    ) -> AuthenticatedIdentity:
        normalized = _normalize_username(username)
        limiter_key = f"{attempt_key.strip()}:{normalized.casefold()}"
        self._limiter.check(limiter_key)
        account = self._local_accounts.get(normalized.casefold())
        encoded_hash = account.password_hash if account is not None else _dummy_password_hash()
        if not verify_password(password, encoded_hash):
            self._limiter.record_failure(limiter_key)
            raise AuthenticationError("invalid credentials")
        assert account is not None
        self._limiter.clear(limiter_key)
        return account.identity

    def resolve_feishu_identity(self, user: FeishuIdentity) -> AuthenticatedIdentity:
        if self._feishu_tenant_key is None or not hmac.compare_digest(
            user.tenant_key,
            self._feishu_tenant_key,
        ):
            raise AuthenticationError("Feishu tenant is not authorized")
        configured = self._feishu_principals.get(user.open_id)
        if configured is None:
            raise AuthenticationError("Feishu user is not authorized")
        return AuthenticatedIdentity(
            principal=configured.principal,
            display_name=(
                configured.display_name
                if configured.display_name != user.open_id
                else user.name
            ),
            avatar_url=user.avatar_url,
            auth_method="feishu",
        )

    def issue_session(
        self,
        identity: AuthenticatedIdentity,
        *,
        now: datetime | None = None,
    ) -> str:
        issued_at = _timestamp(now)
        payload = {
            "v": _COOKIE_PAYLOAD_VERSION,
            "kind": "session",
            "iat": issued_at,
            "exp": issued_at + self._session_ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
            "subject": identity.principal.subject,
            "roles": sorted(identity.principal.roles),
            "allowed_company_ids": sorted(
                str(value) for value in identity.principal.allowed_company_ids
            ),
            "organization_path": identity.principal.organization_path,
            "display_name": identity.display_name,
            "avatar_url": identity.avatar_url,
            "auth_method": identity.auth_method,
        }
        return self._encode(payload)

    def authenticate_session(
        self,
        token: str | None,
        *,
        now: datetime | None = None,
    ) -> AuthenticatedIdentity | None:
        if token is None or self._secret is None:
            return None
        try:
            payload = self._decode(token, expected_kind="session", now=now)
            identity = _identity_from_mapping(
                payload,
                default_subject="",
                default_display_name="",
                auth_method=str(payload.get("auth_method", "session")),
            )
            avatar_url = payload.get("avatar_url")
            if avatar_url is not None and not isinstance(avatar_url, str):
                raise AuthenticationError("invalid session avatar")
            return AuthenticatedIdentity(
                principal=identity.principal,
                display_name=identity.display_name,
                avatar_url=avatar_url,
                auth_method=identity.auth_method,
            )
        except (AuthenticationError, TypeError, ValueError, KeyError):
            return None

    def issue_oauth_transaction(
        self,
        *,
        return_to: str,
        now: datetime | None = None,
    ) -> OAuthTransaction:
        if self._secret is None:
            raise AuthenticationError("OAuth authentication is not configured")
        issued_at = _timestamp(now)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _base64url(sha256(verifier.encode("ascii")).digest())
        cookie_value = self._encode(
            {
                "v": _COOKIE_PAYLOAD_VERSION,
                "kind": "oauth",
                "iat": issued_at,
                "exp": issued_at + self._oauth_state_ttl_seconds,
                "state": state,
                "code_verifier": verifier,
                "return_to": _safe_return_to(return_to),
            }
        )
        return OAuthTransaction(state=state, cookie_value=cookie_value, code_challenge=challenge)

    def consume_oauth_transaction(
        self,
        token: str | None,
        state: str | None,
        *,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        if token is None or state is None:
            raise AuthenticationError("OAuth state is missing")
        payload = self._decode(token, expected_kind="oauth", now=now)
        stored_state = payload.get("state")
        verifier = payload.get("code_verifier")
        return_to = payload.get("return_to")
        if (
            not isinstance(stored_state, str)
            or not hmac.compare_digest(stored_state, state)
            or not isinstance(verifier, str)
            or not 43 <= len(verifier) <= 128
            or not isinstance(return_to, str)
        ):
            raise AuthenticationError("OAuth state is invalid")
        return verifier, _safe_return_to(return_to)

    def _encode(self, payload: Mapping[str, object]) -> str:
        if self._secret is None:
            raise AuthenticationError("session signing is not configured")
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, body, sha256).digest()
        return f"{_base64url(body)}.{_base64url(signature)}"

    def _decode(
        self,
        token: str,
        *,
        expected_kind: str,
        now: datetime | None,
    ) -> dict[str, object]:
        if self._secret is None:
            raise AuthenticationError("session signing is not configured")
        try:
            encoded_body, encoded_signature = token.split(".", maxsplit=1)
            body = _decode_base64url(encoded_body)
            signature = _decode_base64url(encoded_signature)
        except (ValueError, UnicodeError) as error:
            raise AuthenticationError("signed state is malformed") from error
        if len(signature) != _COOKIE_SIGNATURE_BYTES or not hmac.compare_digest(
            hmac.new(self._secret, body, sha256).digest(),
            signature,
        ):
            raise AuthenticationError("signed state signature is invalid")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise AuthenticationError("signed state payload is invalid") from error
        if not isinstance(payload, dict):
            raise AuthenticationError("signed state payload must be an object")
        current = _timestamp(now)
        if (
            payload.get("v") != _COOKIE_PAYLOAD_VERSION
            or payload.get("kind") != expected_kind
            or type(payload.get("iat")) is not int
            or type(payload.get("exp")) is not int
            or cast(int, payload["iat"]) > current + 60
            or cast(int, payload["exp"]) <= current
        ):
            raise AuthenticationError("signed state is expired or invalid")
        return cast(dict[str, object], payload)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    _validate_password(password)
    resolved_salt = salt or secrets.token_bytes(16)
    digest = scrypt(
        password.encode("utf-8"),
        salt=resolved_salt,
        n=_PASSWORD_N,
        r=_PASSWORD_R,
        p=_PASSWORD_P,
        dklen=32,
    )
    return "$".join(
        (
            _PASSWORD_SCHEME,
            str(_PASSWORD_N),
            str(_PASSWORD_R),
            str(_PASSWORD_P),
            _base64url(resolved_salt),
            _base64url(digest),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    if not isinstance(password, str) or len(password) > _PASSWORD_MAX_LENGTH:
        return False
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = encoded_hash.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        if scheme != _PASSWORD_SCHEME or (n, r, p) != (_PASSWORD_N, _PASSWORD_R, _PASSWORD_P):
            return False
        salt = _decode_base64url(salt_text)
        expected = _decode_base64url(digest_text)
        if not 8 <= len(salt) <= 64 or len(expected) != 32:
            return False
        actual = scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _parse_local_accounts(
    accounts: Mapping[str, Mapping[str, object]],
) -> dict[str, _ConfiguredAccount]:
    parsed: dict[str, _ConfiguredAccount] = {}
    for username, mapping in accounts.items():
        normalized = _normalize_username(username)
        password_hash = mapping.get("password_hash")
        if not isinstance(password_hash, str) or not password_hash.strip():
            raise ValueError(f"local account {normalized} requires password_hash")
        if not _valid_password_hash(password_hash.strip()):
            raise ValueError(f"local account {normalized} has an invalid password_hash")
        key = normalized.casefold()
        if key in parsed:
            raise ValueError("local account usernames must be unique ignoring case")
        parsed[key] = _ConfiguredAccount(
            username=normalized,
            password_hash=password_hash.strip(),
            identity=_identity_from_mapping(
                mapping,
                default_subject=f"local:{normalized}",
                default_display_name=normalized,
                auth_method="password",
            ),
        )
    return parsed


def _identity_from_mapping(
    mapping: Mapping[str, object],
    *,
    default_subject: str,
    default_display_name: str,
    auth_method: str,
) -> AuthenticatedIdentity:
    subject = mapping.get("subject", default_subject)
    display_name = mapping.get("display_name", default_display_name)
    roles = mapping.get("roles")
    company_ids = mapping.get("allowed_company_ids", [])
    organization_path = mapping.get("organization_path")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or not isinstance(display_name, str)
        or not display_name.strip()
        or not isinstance(roles, list)
        or not roles
        or not all(isinstance(value, str) for value in roles)
        or not isinstance(company_ids, list)
        or not all(isinstance(value, str) for value in company_ids)
        or not isinstance(organization_path, str)
        or not organization_path.strip()
    ):
        raise ValueError("configured principal has invalid fields")
    normalized_roles = frozenset(cast(list[str], roles))
    if not normalized_roles <= API_ROLES:
        raise ValueError("configured principal contains unsupported roles")
    principal = Principal(
        subject=subject.strip(),
        roles=normalized_roles,
        allowed_company_ids=frozenset(UUID(value) for value in cast(list[str], company_ids)),
        organization_path=organization_path.strip(),
    )
    return AuthenticatedIdentity(
        principal=principal,
        display_name=display_name.strip(),
        avatar_url=None,
        auth_method=auth_method,
    )


def _normalize_username(value: str) -> str:
    if not isinstance(value, str):
        raise AuthenticationError("username must be a string")
    normalized = value.strip()
    if _USERNAME_PATTERN.fullmatch(normalized) is None:
        raise AuthenticationError("username is invalid")
    return normalized


def _valid_password_hash(value: str) -> bool:
    return _password_hash_shape(value)


def _password_hash_shape(value: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = value.split("$")
        return bool(
            scheme == _PASSWORD_SCHEME
            and (int(n_text), int(r_text), int(p_text))
            == (_PASSWORD_N, _PASSWORD_R, _PASSWORD_P)
            and 8 <= len(_decode_base64url(salt_text)) <= 64
            and len(_decode_base64url(digest_text)) == 32
        )
    except (TypeError, ValueError):
        return False


@cache
def _dummy_password_hash() -> str:
    return hash_password(
        "invalid-password-placeholder",
        salt=b"tax-risk-dummy!",
    )


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or not password or len(password) > _PASSWORD_MAX_LENGTH:
        raise ValueError("password must contain between 1 and 1024 characters")


def _safe_return_to(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("/") or normalized.startswith("//") or "\\" in normalized:
        return "/"
    return normalized


def _timestamp(value: datetime | None) -> int:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("authentication timestamps must be timezone-aware")
    return int(resolved.timestamp())


def _base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("base64url value is required")
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)


__all__ = [
    "AuthenticatedIdentity",
    "AuthenticationError",
    "AuthenticationService",
    "FeishuIdentity",
    "LoginAttemptLimiter",
    "LoginRateLimited",
    "OAuthTransaction",
    "hash_password",
    "verify_password",
]

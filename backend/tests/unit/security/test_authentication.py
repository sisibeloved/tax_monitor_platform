from datetime import UTC, datetime, timedelta

import pytest

from tax_risk.security.authentication import (
    AuthenticationError,
    AuthenticationService,
    FeishuIdentity,
    LoginRateLimited,
    hash_password,
    verify_password,
)


def _principal_mapping(password_hash: str | None = None) -> dict[str, object]:
    mapping: dict[str, object] = {
        "subject": "user-1",
        "display_name": "测试用户",
        "roles": ["group-tax"],
        "allowed_company_ids": [],
        "organization_path": "/group/tax",
    }
    if password_hash is not None:
        mapping["password_hash"] = password_hash
    return mapping


def _service(*, max_failures: int = 5) -> AuthenticationService:
    return AuthenticationService(
        session_secret="test-session-secret-at-least-32-characters",
        session_ttl_seconds=3_600,
        oauth_state_ttl_seconds=600,
        local_accounts={"tax.admin": _principal_mapping(hash_password("correct-password"))},
        feishu_principals={"ou_test": _principal_mapping()},
        feishu_tenant_key="tenant-test",
        login_max_failures=max_failures,
        login_window_seconds=300,
    )


def test_hash_password_round_trip_and_rejects_wrong_password() -> None:
    encoded = hash_password("a strong local password", salt=b"0123456789abcdef")

    assert verify_password("a strong local password", encoded)
    assert not verify_password("wrong password", encoded)


def test_password_login_issues_and_validates_a_tamper_evident_session() -> None:
    service = _service()
    now = datetime(2026, 7, 25, tzinfo=UTC)
    identity = service.authenticate_password(
        " TAX.ADMIN ",
        "correct-password",
        attempt_key="127.0.0.1",
    )

    token = service.issue_session(identity, now=now)
    restored = service.authenticate_session(token, now=now + timedelta(minutes=5))

    assert restored == identity
    assert service.authenticate_session(f"{token}x", now=now) is None
    assert service.authenticate_session(token, now=now + timedelta(hours=2)) is None


def test_failed_password_attempts_are_rate_limited() -> None:
    service = _service(max_failures=2)

    for _ in range(2):
        with pytest.raises(AuthenticationError):
            service.authenticate_password("tax.admin", "wrong", attempt_key="client")

    with pytest.raises(LoginRateLimited):
        service.authenticate_password("tax.admin", "correct-password", attempt_key="client")


def test_oauth_transaction_binds_state_and_sanitizes_return_path() -> None:
    service = _service()
    now = datetime(2026, 7, 25, tzinfo=UTC)
    transaction = service.issue_oauth_transaction(
        return_to="https://attacker.example/path",
        now=now,
    )

    verifier, return_to = service.consume_oauth_transaction(
        transaction.cookie_value,
        transaction.state,
        now=now + timedelta(seconds=30),
    )

    assert 43 <= len(verifier) <= 128
    assert return_to == "/"
    with pytest.raises(AuthenticationError):
        service.consume_oauth_transaction(
            transaction.cookie_value,
            "wrong-state",
            now=now,
        )


def test_feishu_identity_requires_the_configured_tenant_and_open_id() -> None:
    service = _service()

    identity = service.resolve_feishu_identity(
        FeishuIdentity(
            open_id="ou_test",
            tenant_key="tenant-test",
            name="飞书用户",
            avatar_url="https://example.test/avatar.png",
        )
    )

    assert identity.principal.subject == "user-1"
    assert identity.avatar_url == "https://example.test/avatar.png"
    with pytest.raises(AuthenticationError):
        service.resolve_feishu_identity(
            FeishuIdentity(open_id="ou_test", tenant_key="other-tenant", name="Other")
        )

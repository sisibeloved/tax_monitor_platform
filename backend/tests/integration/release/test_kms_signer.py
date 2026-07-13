import asyncio
from base64 import b64encode

import httpx

from tax_risk.adapters.signing.kms_ed25519_signer import (
    KmsEd25519Signer,
    KmsSignerConfiguration,
)


def test_kms_signer_uses_workload_identity_allowlist_and_never_receives_private_key() -> None:
    recorded: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["authorization"] = request.headers["Authorization"]
        recorded["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "signature_base64": b64encode(b"s" * 64).decode("ascii"),
                "key_version": "v9",
                "signed_at": "2026-07-13T08:00:00Z",
            },
        )

    signer = KmsEd25519Signer(
        KmsSignerConfiguration(
            endpoint="https://kms.internal.example",
            key_id="prod-release-key",
            allowed_key_ids=frozenset({"prod-release-key"}),
            workload_audience="tax-risk-release",
            environment="production",
        ),
        token_provider=lambda audience: f"token-for-{audience}",
        transport=httpx.MockTransport(handler),
    )

    envelope = asyncio.run(signer.sign_digest("a" * 64))

    assert envelope.key_id == "prod-release-key"
    assert envelope.key_version == "v9"
    assert recorded["authorization"] == "Bearer token-for-tax-risk-release"
    assert '"digest_sha256":"' in str(recorded["body"])
    assert "private" not in str(recorded["body"]).lower()

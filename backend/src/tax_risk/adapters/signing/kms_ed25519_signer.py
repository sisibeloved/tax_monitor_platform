from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
import json
import os
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.release.manifest import ReleaseManifest
from tax_risk.release.signing import SignatureEnvelope
from tax_risk.release.signing import Ed25519ManifestVerifier, TrustedPublicKey


class KmsSignerConfiguration(BaseModel):
    endpoint: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    allowed_key_ids: frozenset[str]
    workload_audience: str = Field(min_length=1)
    environment: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("endpoint")
    @classmethod
    def require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("KMS signing endpoint must use HTTPS")
        return value.rstrip("/")


class KmsEd25519Signer:
    """Request Ed25519 signing through workload identity; private keys never enter the app."""

    def __init__(
        self,
        configuration: KmsSignerConfiguration,
        *,
        token_provider: Callable[[str], str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if configuration.key_id not in configuration.allowed_key_ids:
            raise ValueError("configured release key is not in the KMS allowlist")
        self._configuration = configuration
        self._token_provider = token_provider
        self._transport = transport

    async def sign_digest(self, digest_sha256: str) -> SignatureEnvelope:
        if len(digest_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in digest_sha256
        ):
            raise ValueError("digest_sha256 must be a lowercase SHA-256 digest")

        token = self._token_provider(self._configuration.workload_audience)
        if not token:
            raise ValueError("workload identity provider returned an empty token")
        async with httpx.AsyncClient(
            base_url=self._configuration.endpoint,
            transport=self._transport,
            timeout=10.0,
        ) as client:
            response = await client.post(
                "/v1/ed25519/sign",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "digest_sha256": digest_sha256,
                    "key_id": self._configuration.key_id,
                    "algorithm": "ED25519",
                },
            )
            response.raise_for_status()
            payload = response.json()

        return SignatureEnvelope(
            manifest_sha256=digest_sha256,
            key_id=self._configuration.key_id,
            key_version=payload["key_version"],
            algorithm="ED25519",
            signature_base64=payload["signature_base64"],
            signed_at=datetime.fromisoformat(payload["signed_at"].replace("Z", "+00:00")),
        )

    async def trusted_public_key(self, *, key_version: str) -> TrustedPublicKey:
        token = self._token_provider(self._configuration.workload_audience)
        if not token:
            raise ValueError("workload identity provider returned an empty token")
        async with httpx.AsyncClient(
            base_url=self._configuration.endpoint,
            transport=self._transport,
            timeout=10.0,
        ) as client:
            response = await client.get(
                f"/v1/ed25519/keys/{self._configuration.key_id}/versions/{key_version}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        key = TrustedPublicKey.model_validate(payload)
        if key.key_id != self._configuration.key_id or key.key_version != key_version:
            raise ValueError("KMS public-key identity does not match the signing response")
        return key


def _load_manifest(path: Path) -> ReleaseManifest:
    return ReleaseManifest.model_validate_json(path.read_text(encoding="utf-8"))


def verify_signed_bundle(bundle_path: Path) -> None:
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    manifest = ReleaseManifest.model_validate(payload["manifest"])
    envelope = SignatureEnvelope.model_validate(payload["signature"])
    trusted_key = TrustedPublicKey.model_validate(payload["trusted_public_key"])
    if payload.get("signing_mode") != "PRODUCTION_KMS_HSM":
        raise ValueError("production bundle does not declare KMS/HSM signing")
    Ed25519ManifestVerifier((trusted_key,)).verify(manifest, envelope)


async def sign_manifest_with_kms(
    *,
    manifest_path: Path,
    output_dir: Path,
    configuration: KmsSignerConfiguration,
    workload_token: str,
) -> None:
    manifest = _load_manifest(manifest_path)
    signer = KmsEd25519Signer(
        configuration,
        token_provider=lambda audience: workload_token
        if audience == configuration.workload_audience
        else "",
    )
    envelope = await signer.sign_digest(manifest.manifest_sha256)
    trusted_key = await signer.trusted_public_key(key_version=envelope.key_version)
    Ed25519ManifestVerifier((trusted_key,)).verify(manifest, envelope)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "release-signature.json").write_text(
        json.dumps(envelope.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bundle_path = output_dir / "signed-manifest.json"
    bundle_path.write_text(
        json.dumps(
            {
                "manifest": manifest.model_dump(mode="json"),
                "manifest_sha256": manifest.manifest_sha256,
                "signature": envelope.model_dump(mode="json"),
                "trusted_public_key": trusted_key.model_dump(mode="json"),
                "signing_mode": "PRODUCTION_KMS_HSM",
                "verification_passed": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="通过企业 KMS/HSM 签名或验证发布清单")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sign", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output-dir", type=Path)
    arguments = parser.parse_args()

    if arguments.verify_only:
        if arguments.bundle is None:
            parser.error("--verify-only 需要 --bundle")
        verify_signed_bundle(arguments.bundle)
        return

    if arguments.manifest is None or arguments.output_dir is None:
        parser.error("--sign 需要 --manifest 和 --output-dir")
    key_id = os.environ["KMS_RELEASE_KEY_ID"]
    allowed_key_ids = frozenset(
        item.strip() for item in os.environ["KMS_ALLOWED_KEY_IDS"].split(",") if item.strip()
    )
    configuration = KmsSignerConfiguration(
        endpoint=os.environ["KMS_SIGNING_ENDPOINT"],
        key_id=key_id,
        allowed_key_ids=allowed_key_ids,
        workload_audience=os.environ.get("KMS_WORKLOAD_AUDIENCE", "tax-risk-release"),
        environment="production",
    )
    asyncio.run(
        sign_manifest_with_kms(
            manifest_path=arguments.manifest,
            output_dir=arguments.output_dir,
            configuration=configuration,
            workload_token=os.environ["WORKLOAD_IDENTITY_TOKEN"],
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "KmsEd25519Signer",
    "KmsSignerConfiguration",
    "sign_manifest_with_kms",
    "verify_signed_bundle",
]

from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.release.manifest import ReleaseManifest, SHA256_PATTERN


class SignatureVerificationError(ValueError):
    """Raised when a release signature is not valid under the trusted-key policy."""


class SignatureEnvelope(BaseModel):
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    key_id: str = Field(min_length=1, max_length=256)
    key_version: str = Field(min_length=1, max_length=128)
    algorithm: Literal["ED25519"] = "ED25519"
    signature_base64: str = Field(min_length=1)
    signed_at: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("signed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("signed_at must include a timezone")
        return value


class TrustedPublicKey(BaseModel):
    key_id: str = Field(min_length=1, max_length=256)
    key_version: str = Field(min_length=1, max_length=128)
    public_key_base64: str = Field(min_length=1)
    state: Literal["CURRENT", "PREVIOUS", "RETIRED"]
    valid_from: datetime
    retained_until: datetime | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("valid_from", "retained_until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("trusted-key times must include a timezone")
        return value


class Ed25519ManifestVerifier:
    """Verify a manifest digest against current or explicitly overlapping keys."""

    def __init__(
        self,
        trusted_keys: Iterable[TrustedPublicKey],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trusted_keys = {
            (key.key_id, key.key_version): key for key in trusted_keys
        }
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(
        self,
        manifest: ReleaseManifest,
        envelope: SignatureEnvelope,
    ) -> None:
        if envelope.manifest_sha256 != manifest.manifest_sha256:
            raise SignatureVerificationError("manifest digest does not match the signed envelope")

        key = self._trusted_keys.get((envelope.key_id, envelope.key_version))
        if key is None:
            raise SignatureVerificationError("signature did not use a trusted key")
        if not self._is_active(key, self._clock()):
            raise SignatureVerificationError("trusted key is not active")

        try:
            public_bytes = b64decode(key.public_key_base64, validate=True)
            signature = b64decode(envelope.signature_base64, validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
            public_key.verify(signature, bytes.fromhex(manifest.manifest_sha256))
        except (InvalidSignature, ValueError, binascii.Error) as exc:
            raise SignatureVerificationError("Ed25519 signature verification failed") from exc

    @staticmethod
    def _is_active(key: TrustedPublicKey, now: datetime) -> bool:
        if now < key.valid_from or key.state == "RETIRED":
            return False
        if key.state == "CURRENT":
            return True
        return key.retained_until is not None and now <= key.retained_until


__all__ = [
    "Ed25519ManifestVerifier",
    "SignatureEnvelope",
    "SignatureVerificationError",
    "TrustedPublicKey",
]


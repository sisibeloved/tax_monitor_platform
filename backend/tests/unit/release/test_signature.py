from base64 import b64encode
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tax_risk.release.manifest import ReleaseArtifacts, ReleaseManifest
from tax_risk.release.signing import (
    Ed25519ManifestVerifier,
    SignatureEnvelope,
    SignatureVerificationError,
    TrustedPublicKey,
)


NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


def _manifest(rule_hash: str = "1" * 64) -> ReleaseManifest:
    return ReleaseManifest(
        candidate_version="2026.07.13-rc1",
        application_image_digest=f"sha256:{'a' * 64}",
        git_commit="b" * 40,
        migration_head="0016_release_manifests",
        artifacts=ReleaseArtifacts(
            rule_package_sha256=rule_hash,
            prompt_package_sha256="2" * 64,
            model_adapter_config_sha256="3" * 64,
            account_dictionary_sha256="4" * 64,
            case_library_sha256="5" * 64,
            evaluation_report_sha256="6" * 64,
            replay_report_sha256="7" * 64,
        ),
        created_at=NOW,
    )


def _signed(
    manifest: ReleaseManifest,
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "release-key",
    key_version: str = "v3",
) -> SignatureEnvelope:
    signature = private_key.sign(bytes.fromhex(manifest.manifest_sha256))
    return SignatureEnvelope(
        manifest_sha256=manifest.manifest_sha256,
        key_id=key_id,
        key_version=key_version,
        algorithm="ED25519",
        signature_base64=b64encode(signature).decode("ascii"),
        signed_at=NOW,
    )


def _trusted(
    private_key: Ed25519PrivateKey,
    *,
    state: str = "CURRENT",
    retained_until: datetime | None = None,
) -> TrustedPublicKey:
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return TrustedPublicKey(
        key_id="release-key",
        key_version="v3",
        public_key_base64=b64encode(public_bytes).decode("ascii"),
        state=state,
        valid_from=NOW - timedelta(days=1),
        retained_until=retained_until,
    )


def test_approved_ed25519_public_key_verifies_canonical_manifest_digest() -> None:
    private_key = Ed25519PrivateKey.generate()
    manifest = _manifest()
    verifier = Ed25519ManifestVerifier((_trusted(private_key),), clock=lambda: NOW)

    verifier.verify(manifest, _signed(manifest, private_key))


def test_tampered_manifest_and_unknown_key_are_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    manifest = _manifest()
    envelope = _signed(manifest, private_key)
    verifier = Ed25519ManifestVerifier((_trusted(private_key),), clock=lambda: NOW)

    with pytest.raises(SignatureVerificationError, match="manifest digest"):
        verifier.verify(_manifest(rule_hash="f" * 64), envelope)
    with pytest.raises(SignatureVerificationError, match="trusted key"):
        verifier.verify(
            manifest,
            envelope.model_copy(update={"key_id": "unknown-key"}),
        )


def test_previous_key_is_accepted_only_during_explicit_rotation_overlap() -> None:
    private_key = Ed25519PrivateKey.generate()
    manifest = _manifest()
    envelope = _signed(manifest, private_key)
    overlapping = _trusted(
        private_key,
        state="PREVIOUS",
        retained_until=NOW + timedelta(hours=1),
    )
    expired = overlapping.model_copy(update={"retained_until": NOW - timedelta(seconds=1)})

    Ed25519ManifestVerifier((overlapping,), clock=lambda: NOW).verify(manifest, envelope)
    with pytest.raises(SignatureVerificationError, match="not active"):
        Ed25519ManifestVerifier((expired,), clock=lambda: NOW).verify(manifest, envelope)

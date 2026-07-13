from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tax_risk.release.manifest import ReleaseArtifacts, ReleaseManifest


def _artifacts(**overrides: str) -> ReleaseArtifacts:
    values = {
        "rule_package_sha256": "1" * 64,
        "prompt_package_sha256": "2" * 64,
        "model_adapter_config_sha256": "3" * 64,
        "account_dictionary_sha256": "4" * 64,
        "case_library_sha256": "5" * 64,
        "evaluation_report_sha256": "6" * 64,
        "replay_report_sha256": "7" * 64,
    }
    values.update(overrides)
    return ReleaseArtifacts(**values)


def _manifest(**overrides: object) -> ReleaseManifest:
    values: dict[str, object] = {
        "candidate_version": "2026.07.13-rc1",
        "application_image_digest": f"sha256:{'a' * 64}",
        "git_commit": "b" * 40,
        "migration_head": "0016_release_manifests",
        "artifacts": _artifacts(),
        "created_at": datetime(2026, 7, 13, 8, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return ReleaseManifest(**values)


def test_manifest_is_canonical_and_hashes_every_governed_artifact() -> None:
    manifest = _manifest()

    assert manifest.canonical_bytes() == _manifest().canonical_bytes()
    assert len(manifest.manifest_sha256) == 64
    assert b'"migration_head":"0016_release_manifests"' in manifest.canonical_bytes()
    assert b'"rule_package_sha256":"' in manifest.canonical_bytes()


def test_changing_any_artifact_or_migration_head_changes_manifest_hash() -> None:
    original = _manifest()
    changed_artifact = _manifest(
        artifacts=_artifacts(rule_package_sha256="f" * 64)
    )
    changed_migration = _manifest(migration_head="0017_unapproved")

    assert changed_artifact.manifest_sha256 != original.manifest_sha256
    assert changed_migration.manifest_sha256 != original.manifest_sha256


def test_manifest_rejects_missing_or_non_digest_artifacts() -> None:
    with pytest.raises(ValidationError):
        ReleaseArtifacts(
            rule_package_sha256="not-a-digest",
            prompt_package_sha256="2" * 64,
            model_adapter_config_sha256="3" * 64,
            account_dictionary_sha256="4" * 64,
            case_library_sha256="5" * 64,
            evaluation_report_sha256="6" * 64,
            replay_report_sha256="7" * 64,
        )

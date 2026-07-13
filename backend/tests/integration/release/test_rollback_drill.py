from base64 import b64encode
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tax_risk.release.manifest import ReleaseArtifacts, ReleaseManifest
from tax_risk.release.rollback import (
    DeterministicDrillOperations,
    RollbackInputChanged,
    RollbackInputs,
    RollbackInterrupted,
    RollbackRunner,
    RollbackStage,
)
from tax_risk.release.signing import SignatureEnvelope, TrustedPublicKey


NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


def _signed_bundle(path: Path, version: str, marker: str) -> Path:
    manifest = ReleaseManifest(
        candidate_version=version,
        application_image_digest=f"sha256:{marker * 64}",
        git_commit=marker * 40,
        migration_head="0017_strict_rls_runtime",
        artifacts=ReleaseArtifacts(
            rule_package_sha256="1" * 64,
            prompt_package_sha256="2" * 64,
            model_adapter_config_sha256="3" * 64,
            account_dictionary_sha256="4" * 64,
            case_library_sha256="5" * 64,
            evaluation_report_sha256="6" * 64,
            replay_report_sha256="7" * 64,
        ),
        created_at=NOW,
    )
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    envelope = SignatureEnvelope(
        manifest_sha256=manifest.manifest_sha256,
        key_id=f"test-{version}",
        key_version="v1",
        signature_base64=b64encode(
            private_key.sign(bytes.fromhex(manifest.manifest_sha256))
        ).decode("ascii"),
        signed_at=NOW,
    )
    trusted_key = TrustedPublicKey(
        key_id=envelope.key_id,
        key_version=envelope.key_version,
        public_key_base64=b64encode(public_bytes).decode("ascii"),
        state="CURRENT",
        valid_from=NOW,
    )
    path.write_text(
        json.dumps(
            {
                "manifest": manifest.model_dump(mode="json"),
                "manifest_sha256": manifest.manifest_sha256,
                "signature": envelope.model_dump(mode="json"),
                "trusted_public_key": trusted_key.model_dump(mode="json"),
                "signing_mode": "CI_EPHEMERAL_NOT_FOR_PRODUCTION",
                "verification_passed": True,
            }
        ),
        encoding="utf-8",
    )
    return path


def _inputs(tmp_path: Path) -> RollbackInputs:
    return RollbackInputs(
        candidate_bundle=_signed_bundle(tmp_path / "candidate.json", "candidate", "a"),
        previous_bundle=_signed_bundle(tmp_path / "previous.json", "previous", "b"),
        backup_id="backup-2026q2-001",
        affected_batch_ids=("batch-quarterly", "batch-monthly"),
        environment="acceptance",
        approved_change_id="CHG-2026-0713",
        requested_by="release-operator@example.com",
        approved_by="operations-owner@example.com",
        checkpoint_path=tmp_path / "checkpoint.json",
        report_path=tmp_path / "rollback-report.json",
        isolated_restore_target="isolated-restore-phase4",
        representative_company_code="C001",
    )


def test_rollback_drill_is_idempotent_and_verifies_recovery(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    operations = DeterministicDrillOperations()
    runner = RollbackRunner(operations)

    first = runner.run(inputs)
    execution_counts = operations.execution_counts.copy()
    second = runner.run(inputs)

    assert first.recovery_verified is True
    assert first.duplicate_risk_exposures == 0
    assert first.selected_manifest_sha256 != first.candidate_manifest_sha256
    assert second.drill_id == first.drill_id
    assert operations.execution_counts == execution_counts
    assert all(count == 1 for count in execution_counts.values())


@pytest.mark.parametrize("failure_stage", tuple(RollbackStage))
def test_rollback_drill_resumes_after_every_persisted_checkpoint(
    tmp_path: Path,
    failure_stage: RollbackStage,
) -> None:
    inputs = _inputs(tmp_path)
    operations = DeterministicDrillOperations()
    runner = RollbackRunner(operations)

    with pytest.raises(RollbackInterrupted):
        runner.run(inputs, fail_after_stage=failure_stage)
    report = runner.run(inputs)

    assert report.recovery_verified is True
    assert all(count == 1 for count in operations.execution_counts.values())


def test_checkpoint_cannot_be_reused_for_changed_approved_inputs(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    runner = RollbackRunner(DeterministicDrillOperations())
    runner.run(inputs)

    with pytest.raises(RollbackInputChanged):
        runner.run(inputs.model_copy(update={"backup_id": "different-backup"}))

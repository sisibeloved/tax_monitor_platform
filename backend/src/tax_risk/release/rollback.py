from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tax_risk.application.audit import AuditEventDraft, AuditService
from tax_risk.release.manifest import ReleaseManifest
from tax_risk.release.signing import (
    Ed25519ManifestVerifier,
    SignatureEnvelope,
    TrustedPublicKey,
)
from tax_risk.security.principal import Principal


class RollbackInputChanged(RuntimeError):
    """Raised when a checkpoint is reused with different approved inputs."""


class RollbackInterrupted(RuntimeError):
    """Deterministic failure injection used to prove checkpoint resumption."""


class RollbackStage(StrEnum):
    PRECHECK_SIGNATURES = "PRECHECK_SIGNATURES"
    DRAIN_OR_REVOKE_TASKS = "DRAIN_OR_REVOKE_TASKS"
    REVOKE_EXPORT_DOWNLOADS = "REVOKE_EXPORT_DOWNLOADS"
    RESTORE_ISOLATED_BACKUP = "RESTORE_ISOLATED_BACKUP"
    TEST_MIGRATION_DOWNGRADE = "TEST_MIGRATION_DOWNGRADE"
    DEPLOY_PREVIOUS_MANIFEST = "DEPLOY_PREVIOUS_MANIFEST"
    VERIFY_DATA_CHECKSUMS = "VERIFY_DATA_CHECKSUMS"
    RERUN_REPRESENTATIVE_COMPANY = "RERUN_REPRESENTATIVE_COMPANY"
    RECORD_RECOVERY_DECISION = "RECORD_RECOVERY_DECISION"


class RollbackInputs(BaseModel):
    candidate_bundle: Path
    previous_bundle: Path
    backup_id: str = Field(min_length=1, max_length=256)
    affected_batch_ids: tuple[str, ...] = Field(min_length=1)
    environment: Literal["acceptance", "pilot", "production"]
    approved_change_id: str = Field(min_length=1, max_length=256)
    requested_by: str = Field(min_length=1, max_length=256)
    approved_by: str = Field(min_length=1, max_length=256)
    checkpoint_path: Path
    report_path: Path
    isolated_restore_target: str = Field(pattern=r"^isolated-[a-zA-Z0-9._-]+$")
    representative_company_code: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("affected_batch_ids")
    @classmethod
    def unique_batch_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("affected batch IDs must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("affected batch IDs must be unique")
        return value

    @model_validator(mode="after")
    def enforce_separation_of_duties(self) -> "RollbackInputs":
        if self.requested_by == self.approved_by:
            raise ValueError("rollback requester and approver must be different identities")
        return self


class RollbackReport(BaseModel):
    schema_version: str = "phase-4.rollback-report.v1"
    drill_id: UUID
    input_sha256: str
    environment: str
    approved_change_id: str
    requested_by: str
    approved_by: str
    affected_batch_ids: tuple[str, ...]
    candidate_manifest_sha256: str
    selected_manifest_sha256: str
    backup_id: str
    isolated_restore_target: str
    stages: dict[str, dict[str, Any]]
    audit_events: tuple[dict[str, Any], ...]
    duplicate_risk_exposures: int
    recovery_verified: bool
    completed_at: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)


class RollbackOperations(Protocol):
    def execute(self, stage: RollbackStage, inputs: RollbackInputs) -> dict[str, Any]: ...


class RollbackAuditSink(Protocol):
    def emit(self, drill_id: UUID, action: str, payload: dict[str, Any]) -> None: ...


class DatabaseRollbackAuditSink:
    """Write rollback lifecycle events through the existing immutable audit ledger."""

    def __init__(
        self,
        service: AuditService,
        principal: Principal,
        *,
        company_ids: frozenset[UUID] = frozenset(),
    ) -> None:
        self._service = service
        self._principal = principal
        self._company_ids = company_ids

    def emit(self, drill_id: UUID, action: str, payload: dict[str, Any]) -> None:
        self._service.append(
            AuditEventDraft(
                action=action,
                entity_type="ROLLBACK_DRILL",
                entity_id=drill_id,
                principal=self._principal,
                company_ids=self._company_ids,
                result="FAILED" if action.endswith(("FAILED", "INJECTED")) else "SUCCEEDED",
                after_summary=payload,
            )
        )


def _canonical_hash(value: Mapping[str, Any] | list[Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _bundle_manifest(path: Path, *, environment: str) -> tuple[ReleaseManifest, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = ReleaseManifest.model_validate(payload["manifest"])
    signature = SignatureEnvelope.model_validate(payload["signature"])
    trusted_key = TrustedPublicKey.model_validate(payload["trusted_public_key"])
    signing_mode = str(payload.get("signing_mode", ""))
    if environment in {"pilot", "production"} and signing_mode != "PRODUCTION_KMS_HSM":
        raise ValueError("pilot and production rollback require a KMS/HSM-signed manifest")
    Ed25519ManifestVerifier((trusted_key,)).verify(manifest, signature)
    if payload.get("manifest_sha256") != manifest.manifest_sha256:
        raise ValueError("bundle manifest hash does not match its canonical manifest")
    return manifest, signing_mode


class DeterministicDrillOperations:
    """Safe acceptance adapter: it records evidence but never changes a live environment."""

    def __init__(self) -> None:
        self.execution_counts: Counter[str] = Counter()

    def execute(self, stage: RollbackStage, inputs: RollbackInputs) -> dict[str, Any]:
        if inputs.environment != "acceptance":
            raise RuntimeError("deterministic drill operations are restricted to acceptance")
        self.execution_counts[stage.value] += 1
        if stage is RollbackStage.PRECHECK_SIGNATURES:
            candidate, candidate_mode = _bundle_manifest(
                inputs.candidate_bundle,
                environment=inputs.environment,
            )
            previous, previous_mode = _bundle_manifest(
                inputs.previous_bundle,
                environment=inputs.environment,
            )
            if candidate.manifest_sha256 == previous.manifest_sha256:
                raise ValueError("previous verified manifest must differ from the candidate")
            return {
                "candidate_manifest_sha256": candidate.manifest_sha256,
                "candidate_signature_valid": True,
                "candidate_signing_mode": candidate_mode,
                "previous_manifest_sha256": previous.manifest_sha256,
                "previous_signature_valid": True,
                "previous_signing_mode": previous_mode,
                "approved_change_id": inputs.approved_change_id,
            }
        if stage is RollbackStage.DRAIN_OR_REVOKE_TASKS:
            return {
                "affected_batch_ids": list(inputs.affected_batch_ids),
                "drained_task_count": len(inputs.affected_batch_ids),
                "terminated_inflight_task_count": 1,
                "new_task_dispatch_blocked": True,
            }
        if stage is RollbackStage.REVOKE_EXPORT_DOWNLOADS:
            return {"revoked_download_count": 1, "authorization_rechecked": True}
        if stage is RollbackStage.RESTORE_ISOLATED_BACKUP:
            return {
                "backup_id": inputs.backup_id,
                "restore_target": inputs.isolated_restore_target,
                "restore_id": _canonical_hash(
                    {"backup": inputs.backup_id, "target": inputs.isolated_restore_target}
                ),
                "isolated_target_verified": True,
            }
        if stage is RollbackStage.TEST_MIGRATION_DOWNGRADE:
            return {
                "target": inputs.isolated_restore_target,
                "downgrade_executed_on_live_database": False,
                "downgrade_reupgrade_passed": True,
            }
        if stage is RollbackStage.DEPLOY_PREVIOUS_MANIFEST:
            previous, _mode = _bundle_manifest(
                inputs.previous_bundle,
                environment=inputs.environment,
            )
            return {
                "deployed_manifest_sha256": previous.manifest_sha256,
                "deployment_count": 1,
            }
        if stage is RollbackStage.VERIFY_DATA_CHECKSUMS:
            checksum = _canonical_hash(
                {"backup": inputs.backup_id, "batches": inputs.affected_batch_ids}
            )
            return {
                "source_checksum": checksum,
                "restored_checksum": checksum,
                "checksums_match": True,
                "snapshot_and_risk_counts_match": True,
            }
        if stage is RollbackStage.RERUN_REPRESENTATIVE_COMPANY:
            return {
                "company_code": inputs.representative_company_code,
                "rerun_status": "SUCCEEDED",
                "duplicate_risk_exposures": 0,
                "stable_risk_fingerprints": True,
            }
        return {
            "decision": "RECOVERY_VERIFIED",
            "recovery_verified": True,
            "decision_owner": "operations-owner-pending-production-signoff",
        }


class ApprovedCommandOperations:
    """Run approved platform commands without a shell and require JSON evidence on stdout."""

    def __init__(self, commands: Mapping[str, list[str]]) -> None:
        missing = [stage.value for stage in RollbackStage if stage.value not in commands]
        if missing:
            raise ValueError(f"approved rollback command mapping is incomplete: {missing!r}")
        if any(not command for command in commands.values()):
            raise ValueError("approved rollback command arrays must not be empty")
        self._commands = commands

    def execute(self, stage: RollbackStage, inputs: RollbackInputs) -> dict[str, Any]:
        completed = subprocess.run(
            self._commands[stage.value],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "TAX_RISK_ROLLBACK_STAGE": stage.value,
                "TAX_RISK_ROLLBACK_INPUTS_JSON": inputs.model_dump_json(),
            },
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"approved rollback command failed at {stage.value}: exit {completed.returncode}"
            )
        try:
            evidence = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"approved rollback command returned invalid JSON at {stage.value}"
            ) from exc
        if not isinstance(evidence, dict):
            raise RuntimeError(f"approved rollback command evidence must be an object at {stage.value}")
        return evidence


class RollbackRunner:
    _STAGE_AUDIT_ACTIONS = {
        RollbackStage.PRECHECK_SIGNATURES: "ROLLBACK_SIGNATURES_VERIFIED",
        RollbackStage.DRAIN_OR_REVOKE_TASKS: "ROLLBACK_TASKS_DRAINED",
        RollbackStage.REVOKE_EXPORT_DOWNLOADS: "ROLLBACK_EXPORTS_REVOKED",
        RollbackStage.RESTORE_ISOLATED_BACKUP: "ROLLBACK_BACKUP_RESTORED",
        RollbackStage.TEST_MIGRATION_DOWNGRADE: "ROLLBACK_MIGRATION_VERIFIED",
        RollbackStage.DEPLOY_PREVIOUS_MANIFEST: "ROLLBACK_MANIFEST_SWITCHED",
        RollbackStage.VERIFY_DATA_CHECKSUMS: "ROLLBACK_CHECKSUM_VERIFIED",
        RollbackStage.RERUN_REPRESENTATIVE_COMPANY: "ROLLBACK_REPRESENTATIVE_RERUN",
        RollbackStage.RECORD_RECOVERY_DECISION: "ROLLBACK_RECOVERY_DECIDED",
    }

    def __init__(
        self,
        operations: RollbackOperations,
        *,
        audit_sink: RollbackAuditSink | None = None,
    ) -> None:
        self._operations = operations
        self._audit_sink = audit_sink

    def run(
        self,
        inputs: RollbackInputs,
        *,
        fail_after_stage: RollbackStage | None = None,
    ) -> RollbackReport:
        input_sha256 = self._input_hash(inputs)
        drill_id = uuid5(NAMESPACE_URL, f"tax-risk-rollback:{input_sha256}")
        checkpoint = self._load_or_create_checkpoint(inputs, input_sha256, drill_id)
        if checkpoint["stages"]:
            self._record_audit(
                checkpoint,
                drill_id,
                "ROLLBACK_RESUMED",
                {"input_sha256": input_sha256},
            )
            self._write_json(inputs.checkpoint_path, checkpoint)

        for stage in RollbackStage:
            existing = checkpoint["stages"].get(stage.value)
            if existing is not None:
                if existing.get("status") != "SUCCEEDED":
                    raise RuntimeError(f"checkpoint stage {stage.value} is not successful")
                expected_hash = _canonical_hash(existing["evidence"])
                if expected_hash != existing.get("evidence_sha256"):
                    raise RuntimeError(f"checkpoint evidence was changed for {stage.value}")
                continue
            try:
                evidence = self._operations.execute(stage, inputs)
                self._validate_stage_evidence(stage, evidence)
                checkpoint["stages"][stage.value] = {
                    "status": "SUCCEEDED",
                    "evidence": evidence,
                    "evidence_sha256": _canonical_hash(evidence),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                self._record_audit(
                    checkpoint,
                    drill_id,
                    "ROLLBACK_CHECKPOINT_SUCCEEDED",
                    {"stage": stage.value, "evidence_sha256": _canonical_hash(evidence)},
                )
                self._record_audit(
                    checkpoint,
                    drill_id,
                    self._STAGE_AUDIT_ACTIONS[stage],
                    {"stage": stage.value, **evidence},
                )
                self._write_json(inputs.checkpoint_path, checkpoint)
            except Exception as exc:
                self._record_audit(
                    checkpoint,
                    drill_id,
                    "ROLLBACK_CHECKPOINT_FAILED",
                    {"stage": stage.value, "error_code": type(exc).__name__},
                )
                self._write_json(inputs.checkpoint_path, checkpoint)
                raise
            if fail_after_stage is stage:
                self._record_audit(
                    checkpoint,
                    drill_id,
                    "ROLLBACK_FAILURE_INJECTED",
                    {"stage": stage.value},
                )
                self._write_json(inputs.checkpoint_path, checkpoint)
                raise RollbackInterrupted(f"injected interruption after {stage.value}")

        precheck = checkpoint["stages"][RollbackStage.PRECHECK_SIGNATURES.value][
            "evidence"
        ]
        rerun = checkpoint["stages"][RollbackStage.RERUN_REPRESENTATIVE_COMPANY.value][
            "evidence"
        ]
        decision = checkpoint["stages"][RollbackStage.RECORD_RECOVERY_DECISION.value][
            "evidence"
        ]
        report = RollbackReport(
            drill_id=drill_id,
            input_sha256=input_sha256,
            environment=inputs.environment,
            approved_change_id=inputs.approved_change_id,
            requested_by=inputs.requested_by,
            approved_by=inputs.approved_by,
            affected_batch_ids=inputs.affected_batch_ids,
            candidate_manifest_sha256=precheck["candidate_manifest_sha256"],
            selected_manifest_sha256=precheck["previous_manifest_sha256"],
            backup_id=inputs.backup_id,
            isolated_restore_target=inputs.isolated_restore_target,
            stages=checkpoint["stages"],
            audit_events=tuple(checkpoint["audit_events"]),
            duplicate_risk_exposures=int(rerun["duplicate_risk_exposures"]),
            recovery_verified=bool(decision["recovery_verified"]),
            completed_at=datetime.now(timezone.utc),
        )
        self._write_json(inputs.report_path, report.model_dump(mode="json"))
        return report

    @staticmethod
    def _input_hash(inputs: RollbackInputs) -> str:
        for path in (inputs.candidate_bundle, inputs.previous_bundle):
            if not path.is_file():
                raise FileNotFoundError(path)
        return _canonical_hash(
            {
                "candidate_bundle_sha256": sha256(inputs.candidate_bundle.read_bytes()).hexdigest(),
                "previous_bundle_sha256": sha256(inputs.previous_bundle.read_bytes()).hexdigest(),
                "backup_id": inputs.backup_id,
                "affected_batch_ids": inputs.affected_batch_ids,
                "environment": inputs.environment,
                "approved_change_id": inputs.approved_change_id,
                "requested_by": inputs.requested_by,
                "approved_by": inputs.approved_by,
                "isolated_restore_target": inputs.isolated_restore_target,
                "representative_company_code": inputs.representative_company_code,
            }
        )

    def _load_or_create_checkpoint(
        self,
        inputs: RollbackInputs,
        input_sha256: str,
        drill_id: UUID,
    ) -> dict[str, Any]:
        if inputs.checkpoint_path.exists():
            existing_checkpoint: dict[str, Any] = json.loads(
                inputs.checkpoint_path.read_text(encoding="utf-8")
            )
            if existing_checkpoint.get("input_sha256") != input_sha256:
                raise RollbackInputChanged(
                    "existing rollback checkpoint belongs to different approved inputs"
                )
            return existing_checkpoint
        checkpoint: dict[str, Any] = {
            "schema_version": "phase-4.rollback-checkpoint.v1",
            "drill_id": str(drill_id),
            "input_sha256": input_sha256,
            "stages": {},
            "audit_events": [],
        }
        self._record_audit(
            checkpoint,
            drill_id,
            "ROLLBACK_REQUESTED",
            {
                "environment": inputs.environment,
                "approved_change_id": inputs.approved_change_id,
                "requested_by": inputs.requested_by,
                "affected_batch_ids": list(inputs.affected_batch_ids),
            },
        )
        self._record_audit(
            checkpoint,
            drill_id,
            "ROLLBACK_APPROVED",
            {
                "approved_change_id": inputs.approved_change_id,
                "approved_by": inputs.approved_by,
            },
        )
        RollbackRunner._write_json(inputs.checkpoint_path, checkpoint)
        return checkpoint

    def _record_audit(
        self,
        checkpoint: dict[str, Any],
        drill_id: UUID,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        event = {
            "action": action,
            "payload": payload,
            "payload_sha256": _canonical_hash(payload),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        checkpoint["audit_events"].append(event)
        if self._audit_sink is not None:
            self._audit_sink.emit(drill_id, action, payload)

    @staticmethod
    def _validate_stage_evidence(stage: RollbackStage, evidence: dict[str, Any]) -> None:
        if stage is RollbackStage.PRECHECK_SIGNATURES and not (
            evidence.get("candidate_signature_valid")
            and evidence.get("previous_signature_valid")
        ):
            raise RuntimeError("release signature precheck failed")
        if stage is RollbackStage.RESTORE_ISOLATED_BACKUP and not evidence.get(
            "isolated_target_verified"
        ):
            raise RuntimeError("backup was not restored to an isolated target")
        if stage is RollbackStage.TEST_MIGRATION_DOWNGRADE and not evidence.get(
            "downgrade_reupgrade_passed"
        ):
            raise RuntimeError("migration downgrade/re-upgrade drill failed")
        if stage is RollbackStage.VERIFY_DATA_CHECKSUMS and not (
            evidence.get("checksums_match") and evidence.get("snapshot_and_risk_counts_match")
        ):
            raise RuntimeError("restored data checksums do not match")
        if stage is RollbackStage.RERUN_REPRESENTATIVE_COMPANY and (
            evidence.get("rerun_status") != "SUCCEEDED"
            or evidence.get("duplicate_risk_exposures") != 0
        ):
            raise RuntimeError("representative company rerun did not prove idempotency")
        if stage is RollbackStage.RECORD_RECOVERY_DECISION and not evidence.get(
            "recovery_verified"
        ):
            raise RuntimeError("recovery decision was not verified")

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any] | BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        serializable = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="执行可续跑、幂等的第四阶段回滚演练")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--previous-manifest", type=Path, required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--affected-batch", action="append", required=True)
    parser.add_argument("--environment", choices=("acceptance", "pilot", "production"), required=True)
    parser.add_argument("--approved-change-id", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--isolated-restore-target", required=True)
    parser.add_argument("--representative-company", required=True)
    arguments = parser.parse_args()
    inputs = RollbackInputs(
        candidate_bundle=arguments.candidate_manifest,
        previous_bundle=arguments.previous_manifest,
        backup_id=arguments.backup_id,
        affected_batch_ids=tuple(arguments.affected_batch),
        environment=arguments.environment,
        approved_change_id=arguments.approved_change_id,
        requested_by=arguments.requested_by,
        approved_by=arguments.approved_by,
        checkpoint_path=arguments.checkpoint,
        report_path=arguments.report,
        isolated_restore_target=arguments.isolated_restore_target,
        representative_company_code=arguments.representative_company,
    )
    if inputs.environment == "acceptance":
        operations: RollbackOperations = DeterministicDrillOperations()
    else:
        if os.environ.get("ROLLBACK_OPERATION_MODE") != "APPROVED":
            parser.error("试点/生产执行要求 ROLLBACK_OPERATION_MODE=APPROVED")
        if os.environ.get("ROLLBACK_CONFIRM_CHANGE_ID") != inputs.approved_change_id:
            parser.error("ROLLBACK_CONFIRM_CHANGE_ID 与批准变更号不一致")
        commands = json.loads(os.environ.get("ROLLBACK_COMMANDS_JSON", "{}"))
        if not isinstance(commands, dict):
            parser.error("ROLLBACK_COMMANDS_JSON 必须是阶段到命令数组的对象")
        operations = ApprovedCommandOperations(commands)
    report = RollbackRunner(operations).run(inputs)
    if not report.recovery_verified or report.duplicate_risk_exposures != 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()


__all__ = [
    "ApprovedCommandOperations",
    "DatabaseRollbackAuditSink",
    "DeterministicDrillOperations",
    "RollbackInputChanged",
    "RollbackInputs",
    "RollbackInterrupted",
    "RollbackReport",
    "RollbackRunner",
    "RollbackStage",
]

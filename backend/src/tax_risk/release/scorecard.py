from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from tax_risk.release.manifest import SHA256_PATTERN


class EvidenceReference(BaseModel):
    reference: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=SHA256_PATTERN)
    verified: bool

    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceMetrics(BaseModel):
    formula_accuracy: float = Field(ge=0, le=1)
    traceability_rate: float = Field(ge=0, le=1)
    master_data_block_rate: float = Field(ge=0, le=1)
    valid_company_success_rate: float = Field(ge=0, le=1)
    semantic_recall: float = Field(ge=0, le=1)
    high_confidence_accuracy: float = Field(ge=0, le=1)
    known_semantic_misses: int = Field(ge=0)
    maximum_delivery_hours: float = Field(ge=0)
    authorization_isolation_passed: bool
    external_semantic_index_configured: bool
    audit_immutability_passed: bool
    signature_verified: bool
    recovery_verified: bool
    rollback_verified: bool

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScorecardResult(BaseModel):
    snapshot_set_id: str
    evidence_scope: str
    metrics: AcceptanceMetrics
    evidence: dict[str, EvidenceReference]
    approvals: dict[str, str]
    failed_gates: tuple[str, ...]
    technical_ready: bool
    production_ready: bool
    evaluated_at: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductionScorecard:
    _EVIDENCE_KEYS: ClassVar[tuple[str, ...]] = (
        "formula_accuracy",
        "traceability",
        "master_data_blocking",
        "group_capacity",
        "semantic_quality",
        "delivery_timeliness",
        "authorization_isolation",
        "external_index_control",
        "audit_immutability",
        "signature_verification",
        "recovery",
        "rollback",
    )
    _APPROVAL_ROLES: ClassVar[tuple[str, ...]] = (
        "tax_owner",
        "data_owner",
        "security_owner",
        "operations_owner",
    )

    @classmethod
    def required_evidence_keys(cls) -> tuple[str, ...]:
        return cls._EVIDENCE_KEYS

    def evaluate(
        self,
        *,
        metrics: AcceptanceMetrics,
        evidence: dict[str, EvidenceReference],
        approvals: dict[str, str],
        snapshot_set_id: str,
        evidence_scope: str = "PILOT_PRODUCTION",
        evaluated_at: datetime | None = None,
    ) -> ScorecardResult:
        failures: list[str] = []
        exact_metrics = (
            (metrics.formula_accuracy, "FORMULA_ACCURACY_NOT_100_PERCENT"),
            (metrics.traceability_rate, "TRACEABILITY_NOT_100_PERCENT"),
            (metrics.master_data_block_rate, "MASTER_DATA_BLOCK_RATE_NOT_100_PERCENT"),
        )
        failures.extend(code for value, code in exact_metrics if value < 1.0)
        if metrics.valid_company_success_rate < 0.98:
            failures.append("VALID_COMPANY_SUCCESS_RATE_BELOW_98_PERCENT")
        if metrics.semantic_recall < 0.95:
            failures.append("PRODUCTION_RECALL_BELOW_95_PERCENT")
        if metrics.high_confidence_accuracy < 0.80:
            failures.append("HIGH_CONFIDENCE_ACCURACY_BELOW_80_PERCENT")
        if metrics.known_semantic_misses:
            failures.append("KNOWN_SEMANTIC_CASE_MISSED")
        if metrics.maximum_delivery_hours > 48:
            failures.append("DELIVERY_EXCEEDED_48_HOURS")
        boolean_gates = (
            (metrics.authorization_isolation_passed, "AUTHORIZATION_ISOLATION_FAILED"),
            (not metrics.external_semantic_index_configured, "EXTERNAL_INDEX_CONFIGURED"),
            (metrics.audit_immutability_passed, "AUDIT_IMMUTABILITY_FAILED"),
            (metrics.signature_verified, "SIGNATURE_VERIFICATION_FAILED"),
            (metrics.recovery_verified, "RECOVERY_NOT_VERIFIED"),
            (metrics.rollback_verified, "ROLLBACK_NOT_VERIFIED"),
        )
        failures.extend(code for passed, code in boolean_gates if not passed)

        for key in self._EVIDENCE_KEYS:
            reference = evidence.get(key)
            if reference is None:
                failures.append(f"MISSING_EVIDENCE_{key.upper()}")
            elif not reference.verified:
                failures.append(f"UNVERIFIED_EVIDENCE_{key.upper()}")

        technical_failures = tuple(failures)
        if evidence_scope != "PILOT_PRODUCTION":
            failures.append("NON_PRODUCTION_EVIDENCE_SCOPE")
        for role in self._APPROVAL_ROLES:
            if not approvals.get(role, "").strip():
                failures.append(f"MISSING_APPROVAL_{role.upper()}")

        return ScorecardResult(
            snapshot_set_id=snapshot_set_id,
            evidence_scope=evidence_scope,
            metrics=metrics,
            evidence=evidence,
            approvals=approvals,
            failed_gates=tuple(failures),
            technical_ready=not technical_failures,
            production_ready=not failures,
            evaluated_at=evaluated_at or datetime.now(timezone.utc),
        )


def _evidence_reference(path: Path) -> EvidenceReference:
    return EvidenceReference(
        reference=str(path),
        sha256=sha256(path.read_bytes()).hexdigest(),
        verified=True,
    )


def scorecard_from_artifacts(
    *,
    artifact_dir: Path,
    snapshot_set_id: str,
    approvals: dict[str, str],
    evidence_scope: str,
) -> ScorecardResult:
    replay_path = artifact_dir / "replay-report.json"
    capacity_path = artifact_dir / "capacity-report.json"
    governance_path = artifact_dir / "governance.xml"
    security_path = artifact_dir / "security.json"
    signature_path = artifact_dir / "signed-manifest.json"
    rollback_path = artifact_dir / "rollback-report.json"
    required_files = (
        replay_path,
        capacity_path,
        governance_path,
        security_path,
        signature_path,
        rollback_path,
    )
    missing = [str(path) for path in required_files if not path.is_file() or not path.stat().st_size]
    if missing:
        raise FileNotFoundError(f"验收证据缺失或为空：{missing!r}")

    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    capacity = json.loads(capacity_path.read_text(encoding="utf-8"))
    security = json.loads(security_path.read_text(encoding="utf-8"))
    signed_manifest = json.loads(signature_path.read_text(encoding="utf-8"))
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    replay_metrics = replay["metrics"]
    failure_isolation = capacity["checks"]["failure_isolation"]
    t_plus_2 = capacity["checks"]["t_plus_2"]

    metrics = AcceptanceMetrics(
        formula_accuracy=replay_metrics["formula_accuracy"],
        traceability_rate=replay_metrics["traceability_rate"],
        master_data_block_rate=replay_metrics["master_data_block_rate"],
        valid_company_success_rate=failure_isolation["success_rate"],
        semantic_recall=replay_metrics["semantic_recall"],
        high_confidence_accuracy=replay_metrics["high_confidence_accuracy"],
        known_semantic_misses=replay_metrics["known_semantic_misses"],
        maximum_delivery_hours=(48.0 if t_plus_2["passed"] else 48.000001),
        authorization_isolation_passed=security["authorization_rls_isolation"],
        external_semantic_index_configured=security[
            "external_semantic_index_configured"
        ],
        audit_immutability_passed=security["audit_immutability"],
        signature_verified=signed_manifest["verification_passed"],
        recovery_verified=rollback["recovery_verified"],
        rollback_verified=rollback["recovery_verified"]
        and rollback["duplicate_risk_exposures"] == 0,
    )
    evidence_paths = {
        "formula_accuracy": replay_path,
        "traceability": replay_path,
        "master_data_blocking": replay_path,
        "group_capacity": capacity_path,
        "semantic_quality": replay_path,
        "delivery_timeliness": capacity_path,
        "authorization_isolation": governance_path,
        "external_index_control": security_path,
        "audit_immutability": security_path,
        "signature_verification": signature_path,
        "recovery": rollback_path,
        "rollback": rollback_path,
    }
    return ProductionScorecard().evaluate(
        metrics=metrics,
        evidence={key: _evidence_reference(path) for key, path in evidence_paths.items()},
        approvals=approvals,
        snapshot_set_id=snapshot_set_id,
        evidence_scope=evidence_scope,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总第四阶段验收评分卡")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--snapshot-set", required=True)
    parser.add_argument("--approvals-json", default="{}")
    parser.add_argument("--evidence-scope", default="LOCAL_SYNTHETIC")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-production-ready", action="store_true")
    arguments = parser.parse_args()
    approvals = json.loads(arguments.approvals_json)
    if not isinstance(approvals, dict):
        parser.error("--approvals-json 必须是对象")
    result = scorecard_from_artifacts(
        artifact_dir=arguments.artifact_dir,
        snapshot_set_id=arguments.snapshot_set,
        approvals={str(key): str(value) for key, value in approvals.items()},
        evidence_scope=arguments.evidence_scope,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    if not result.technical_ready:
        raise SystemExit(2)
    if arguments.require_production_ready and not result.production_ready:
        raise SystemExit(3)


if __name__ == "__main__":
    main()


__all__ = [
    "AcceptanceMetrics",
    "EvidenceReference",
    "ProductionScorecard",
    "ScorecardResult",
    "scorecard_from_artifacts",
]

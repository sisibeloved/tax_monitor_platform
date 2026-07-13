from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ReleaseStage = Literal["PILOT", "PRODUCTION"]


class ReplayMetrics(BaseModel):
    formula_accuracy: float = Field(ge=0, le=1)
    traceability_rate: float = Field(ge=0, le=1)
    master_data_block_rate: float = Field(ge=0, le=1)
    known_semantic_misses: int = Field(ge=0)
    semantic_recall: float = Field(ge=0, le=1)
    high_confidence_accuracy: float = Field(ge=0, le=1)
    group_batch_success_rate: float = Field(ge=0, le=1)
    security_checks_passed: bool
    migration_checks_passed: bool
    rollback_checks_passed: bool

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayDecision(BaseModel):
    approved: bool
    stage: ReleaseStage
    failure_codes: tuple[str, ...]

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayGate:
    """Fail closed unless all accuracy, resilience, and safety gates pass."""

    def evaluate(self, metrics: ReplayMetrics, *, stage: ReleaseStage) -> ReplayDecision:
        failures: list[str] = []
        exact_thresholds = (
            (metrics.formula_accuracy, "FORMULA_ACCURACY_NOT_100_PERCENT"),
            (metrics.traceability_rate, "TRACEABILITY_NOT_100_PERCENT"),
            (metrics.master_data_block_rate, "MASTER_DATA_BLOCK_RATE_NOT_100_PERCENT"),
        )
        failures.extend(code for value, code in exact_thresholds if value < 1.0)
        if metrics.known_semantic_misses != 0:
            failures.append("KNOWN_SEMANTIC_CASE_MISSED")

        minimum_recall = 0.90 if stage == "PILOT" else 0.95
        if metrics.semantic_recall < minimum_recall:
            failures.append(
                "PILOT_RECALL_BELOW_90_PERCENT"
                if stage == "PILOT"
                else "PRODUCTION_RECALL_BELOW_95_PERCENT"
            )
        if metrics.high_confidence_accuracy < 0.80:
            failures.append("HIGH_CONFIDENCE_ACCURACY_BELOW_80_PERCENT")
        if metrics.group_batch_success_rate < 0.98:
            failures.append("GROUP_BATCH_SUCCESS_RATE_BELOW_98_PERCENT")
        if not metrics.security_checks_passed:
            failures.append("SECURITY_CHECK_FAILED")
        if not metrics.migration_checks_passed:
            failures.append("MIGRATION_CHECK_FAILED")
        if not metrics.rollback_checks_passed:
            failures.append("ROLLBACK_CHECK_FAILED")

        return ReplayDecision(
            approved=not failures,
            stage=stage,
            failure_codes=tuple(failures),
        )


__all__ = ["ReleaseStage", "ReplayDecision", "ReplayGate", "ReplayMetrics"]


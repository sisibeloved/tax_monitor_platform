from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from tax_risk.release.replay_gate import (
    ReleaseStage,
    ReplayDecision,
    ReplayGate,
    ReplayMetrics,
)


class ReplayReport(BaseModel):
    snapshot_set_id: UUID
    stage: ReleaseStage
    metrics: ReplayMetrics
    decision: ReplayDecision
    evaluated_at: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def report_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


class ReplayRunner:
    def __init__(
        self,
        *,
        evaluator: Callable[[UUID], ReplayMetrics],
        gate: ReplayGate,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._gate = gate
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, *, snapshot_set_id: UUID, stage: ReleaseStage) -> ReplayReport:
        metrics = self._evaluator(snapshot_set_id)
        return ReplayReport(
            snapshot_set_id=snapshot_set_id,
            stage=stage,
            metrics=metrics,
            decision=self._gate.evaluate(metrics, stage=stage),
            evaluated_at=self._clock(),
        )


__all__ = ["ReplayReport", "ReplayRunner"]


from datetime import datetime, timezone
from uuid import UUID

from tax_risk.release.replay_gate import ReplayGate, ReplayMetrics
from tax_risk.release.replay_runner import ReplayRunner


SNAPSHOT_SET = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


def _passing_metrics(**overrides: object) -> ReplayMetrics:
    values: dict[str, object] = {
        "formula_accuracy": 1.0,
        "traceability_rate": 1.0,
        "master_data_block_rate": 1.0,
        "known_semantic_misses": 0,
        "semantic_recall": 0.96,
        "high_confidence_accuracy": 0.82,
        "group_batch_success_rate": 0.99,
        "security_checks_passed": True,
        "migration_checks_passed": True,
        "rollback_checks_passed": True,
    }
    values.update(overrides)
    return ReplayMetrics(**values)


def test_production_replay_gate_requires_every_threshold_and_is_deterministic() -> None:
    runner = ReplayRunner(
        evaluator=lambda _snapshot: _passing_metrics(),
        gate=ReplayGate(),
        clock=lambda: NOW,
    )

    first = runner.run(snapshot_set_id=SNAPSHOT_SET, stage="PRODUCTION")
    second = runner.run(snapshot_set_id=SNAPSHOT_SET, stage="PRODUCTION")

    assert first.decision.approved is True
    assert first.report_sha256 == second.report_sha256


def test_replay_gate_rejects_any_missing_evidence_or_threshold() -> None:
    decision = ReplayGate().evaluate(
        _passing_metrics(
            formula_accuracy=0.999,
            known_semantic_misses=1,
            semantic_recall=0.949,
            security_checks_passed=False,
        ),
        stage="PRODUCTION",
    )

    assert decision.approved is False
    assert set(decision.failure_codes) >= {
        "FORMULA_ACCURACY_NOT_100_PERCENT",
        "KNOWN_SEMANTIC_CASE_MISSED",
        "PRODUCTION_RECALL_BELOW_95_PERCENT",
        "SECURITY_CHECK_FAILED",
    }

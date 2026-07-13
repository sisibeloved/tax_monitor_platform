from __future__ import annotations

from pathlib import Path

from tax_risk.application.business_entertainment.golden import load_golden_dataset
from tax_risk.application.business_entertainment.metrics import evaluate_release_metrics


GOLDEN_PATH = (
    Path(__file__).parents[1] / "fixtures" / "business_entertainment" / "golden.jsonl"
)


def test_release_metrics_meet_thresholds_by_semantic_source_mode() -> None:
    report = evaluate_release_metrics(load_golden_dataset(GOLDEN_PATH))

    assert report.overall.candidate_recall >= 0.90
    assert report.overall.model_recall >= 0.95
    assert report.overall.reviewed_recall >= 0.95
    assert report.overall.high_confidence_accuracy >= 0.80
    assert report.overall.known_case_misses == 0
    assert report.negative_sample_count >= 2
    assert set(report.by_source_mode) == {
        "SAP_LINKED",
        "BUSINESS_DOCUMENT_UNLINKED",
    }
    for metrics in report.by_source_mode.values():
        assert metrics.candidate_recall >= 0.90
        assert metrics.model_recall >= 0.95
        assert metrics.reviewed_recall >= 0.95
        assert metrics.high_confidence_accuracy >= 0.80
        assert metrics.known_case_misses == 0

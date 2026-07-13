"""基于冻结黄金集的确定性发布指标。"""

from __future__ import annotations

from dataclasses import dataclass

from tax_risk.application.business_entertainment.golden import (
    GoldenDataset,
    GoldenRecord,
)


@dataclass(frozen=True, slots=True)
class SourceModeMetrics:
    candidate_recall: float
    model_recall: float
    reviewed_recall: float
    high_confidence_accuracy: float
    known_case_misses: int


@dataclass(frozen=True, slots=True)
class ReleaseMetricsReport:
    overall: SourceModeMetrics
    by_source_mode: dict[str, SourceModeMetrics]
    negative_sample_count: int


def evaluate_release_metrics(dataset: GoldenDataset) -> ReleaseMetricsReport:
    semantic_records = tuple(record for record in dataset.records if record.is_semantic_sample)
    by_mode = {
        source_mode: _metrics(
            tuple(record for record in semantic_records if record.source_mode == source_mode)
        )
        for source_mode in ("SAP_LINKED", "BUSINESS_DOCUMENT_UNLINKED")
    }
    return ReleaseMetricsReport(
        overall=_metrics(semantic_records),
        by_source_mode=by_mode,
        negative_sample_count=sum(not record.expected_alert for record in semantic_records),
    )


def _metrics(records: tuple[GoldenRecord, ...]) -> SourceModeMetrics:
    positives = tuple(record for record in records if record.expected_alert)
    high_confidence = tuple(
        record for record in records if record.confidence_tier == "HIGH"
    )
    return SourceModeMetrics(
        candidate_recall=_ratio(
            sum(record.candidate_hit for record in positives), len(positives)
        ),
        model_recall=_ratio(
            sum(record.model_label == record.final_label for record in positives),
            len(positives),
        ),
        reviewed_recall=_ratio(
            sum(record.reviewed_label == record.final_label for record in positives),
            len(positives),
        ),
        high_confidence_accuracy=_ratio(
            sum(record.model_label == record.final_label for record in high_confidence),
            len(high_confidence),
        ),
        known_case_misses=sum(
            record.known_case
            and record.expected_alert
            and record.reviewed_label != record.final_label
            for record in records
        ),
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        raise ValueError("release metrics require positive and high-confidence samples")
    return numerator / denominator


__all__ = [
    "ReleaseMetricsReport",
    "SourceModeMetrics",
    "evaluate_release_metrics",
]

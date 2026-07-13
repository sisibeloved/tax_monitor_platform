"""Small Prometheus text registry with a strict bounded-label policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
import re
from threading import Lock
from typing import Mapping


_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_BOUNDED_VALUE = re.compile(r"^[A-Za-z0-9_./:-]{1,128}$")
_FORBIDDEN_LABELS = frozenset(
    {
        "company_name",
        "company_code",
        "free_text",
        "summary",
        "description",
        "error_message",
        "reason_text",
        "subject",
        "document_id",
    }
)


@dataclass(slots=True)
class Metric:
    name: str
    help_text: str
    metric_type: str
    label_names: tuple[str, ...]
    buckets: tuple[float, ...] = ()
    _values: dict[tuple[str, ...], float] = field(default_factory=dict)
    _histograms: dict[tuple[str, ...], list[float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def inc(self, labels: Mapping[str, str], amount: float = 1.0) -> None:
        if self.metric_type != "counter":
            raise TypeError(f"{self.name} is not a counter")
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def set(self, labels: Mapping[str, str], value: float) -> None:
        if self.metric_type != "gauge":
            raise TypeError(f"{self.name} is not a gauge")
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value

    def observe(self, labels: Mapping[str, str], value: float) -> None:
        if self.metric_type != "histogram":
            raise TypeError(f"{self.name} is not a histogram")
        if value < 0:
            raise ValueError("histogram observations must be non-negative")
        key = self._label_key(labels)
        with self._lock:
            self._histograms.setdefault(key, []).append(value)

    def _label_key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.label_names):
            raise ValueError(
                f"metric labels must exactly match {self.label_names}; got {tuple(labels)}"
            )
        values = tuple(str(labels[name]) for name in self.label_names)
        if any(not _BOUNDED_VALUE.fullmatch(value) for value in values):
            raise ValueError("metric label values must be bounded machine codes")
        return values

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.metric_type}"]
        with self._lock:
            if self.metric_type == "histogram":
                for key in sorted(self._histograms):
                    values = self._histograms[key]
                    cumulative = 0
                    for bucket in (*self.buckets, inf):
                        cumulative = sum(value <= bucket for value in values)
                        bucket_labels = self._format_labels(
                            key,
                            extra=("le", "+Inf" if bucket == inf else _number(bucket)),
                        )
                        lines.append(f"{self.name}_bucket{bucket_labels} {cumulative}")
                    labels = self._format_labels(key)
                    lines.append(f"{self.name}_count{labels} {len(values)}")
                    lines.append(f"{self.name}_sum{labels} {_number(sum(values))}")
            else:
                for key in sorted(self._values):
                    lines.append(
                        f"{self.name}{self._format_labels(key)} {_number(self._values[key])}"
                    )
        return lines

    def _format_labels(
        self,
        values: tuple[str, ...],
        *,
        extra: tuple[str, str] | None = None,
    ) -> str:
        labels = list(zip(self.label_names, values, strict=True))
        if extra is not None:
            labels.append(extra)
        if not labels:
            return ""
        body = ",".join(f'{name}="{value}"' for name, value in labels)
        return "{" + body + "}"


class MetricRegistry:
    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Metric:
        return self._register(name, help_text, "counter", labels)

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Metric:
        return self._register(name, help_text, "gauge", labels)

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
    ) -> Metric:
        metric = self._register(name, help_text, "histogram", labels)
        metric.buckets = tuple(sorted(set(buckets)))
        return metric

    def metric(self, name: str) -> Metric:
        try:
            return self._metrics[name]
        except KeyError as error:
            raise KeyError(f"metric is not registered: {name}") from error

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for name in sorted(self._metrics):
            lines.extend(self._metrics[name].render())
        return "\n".join(lines) + "\n"

    def _register(
        self,
        name: str,
        help_text: str,
        metric_type: str,
        labels: tuple[str, ...],
    ) -> Metric:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid metric name: {name}")
        if not help_text.strip():
            raise ValueError("metric help text is required")
        if len(set(labels)) != len(labels) or any(
            not _LABEL_NAME.fullmatch(label) for label in labels
        ):
            raise ValueError("metric label names must be valid and unique")
        forbidden = _FORBIDDEN_LABELS.intersection(labels)
        if forbidden:
            raise ValueError(f"forbidden metric label: {sorted(forbidden)[0]}")
        existing = self._metrics.get(name)
        if existing is not None:
            if (existing.metric_type, existing.label_names) != (metric_type, labels):
                raise ValueError(f"metric already registered with a different schema: {name}")
            return existing
        metric = Metric(name, help_text.strip(), metric_type, labels)
        self._metrics[name] = metric
        return metric


def build_default_registry() -> MetricRegistry:
    registry = MetricRegistry()
    registry.gauge("tax_risk_data_source_ready", "Required source readiness.", ("source",))
    registry.counter(
        "tax_risk_quality_block_total", "Data quality blocks.", ("source", "reason_code")
    )
    registry.counter(
        "tax_risk_company_task_total",
        "Company task terminal outcomes.",
        ("run_type", "monitor_type", "status", "error_code"),
    )
    registry.histogram(
        "tax_risk_formula_duration_seconds", "Formula execution duration.", ("formula",)
    )
    registry.counter(
        "tax_risk_semantic_candidate_total", "Semantic candidates.", ("monitor_type",)
    )
    registry.counter(
        "tax_risk_semantic_detection_total",
        "Semantic detections.",
        ("monitor_type", "decision"),
    )
    registry.counter(
        "tax_risk_semantic_error_total", "Semantic failures.", ("monitor_type", "error_code")
    )
    registry.gauge(
        "tax_risk_link_coverage_ratio", "Document-to-voucher link coverage.", ("source_pair",)
    )
    registry.gauge(
        "tax_risk_evidence_backlog", "Evidence review backlog.", ("monitor_type",)
    )
    registry.histogram(
        "tax_risk_case_age_seconds", "Open risk case age.", ("monitor_type", "status")
    )
    registry.counter(
        "tax_risk_export_total", "Export job outcomes.", ("status", "format")
    )
    registry.counter(
        "tax_risk_authorization_failure_total",
        "Authorization failures.",
        ("action", "reason_code"),
    )
    registry.gauge(
        "tax_risk_data_ready_timestamp_seconds",
        "Immutable published snapshot-set timestamp.",
        ("run_type",),
    )
    registry.gauge(
        "tax_risk_output_ready_timestamp_seconds",
        "Validated output readiness timestamp.",
        ("run_type", "scope"),
    )
    registry.counter(
        "tax_risk_http_request_total", "HTTP request outcomes.", ("method", "path", "status")
    )
    registry.gauge(
        "tax_risk_readiness_component", "Readiness component state.", ("component", "code")
    )
    return registry


DEFAULT_METRICS = build_default_registry()


def record_company_task(
    *,
    run_type: str,
    monitor_type: str,
    status: str,
    error_code: str | None,
) -> None:
    DEFAULT_METRICS.metric("tax_risk_company_task_total").inc(
        {
            "run_type": run_type,
            "monitor_type": monitor_type,
            "status": status,
            "error_code": error_code or "NONE",
        }
    )


def _number(value: float) -> str:
    normalized = float(value)
    return str(int(normalized)) if normalized.is_integer() else format(normalized, ".12g")


__all__ = [
    "DEFAULT_METRICS",
    "Metric",
    "MetricRegistry",
    "build_default_registry",
    "record_company_task",
]

"""Composition helpers for the production external fetch runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from tax_risk.adapters.cache.redis_fetch_cache import RedisFetchCache
from tax_risk.application.external_fetch import (
    DgcFetchSource,
    FetchCache,
    FetchCoordinatorConfig,
    FetchObserver,
    FetchOutcome,
    FetchRequest,
    NullFetchCache,
    ParallelFetchCoordinator,
)
from tax_risk.config import Settings
from tax_risk.observability.metrics import MetricRegistry


class MetricFetchObserver:
    """Low-cardinality production metrics without business parameters."""

    def __init__(self, registry: MetricRegistry) -> None:
        self._registry = registry
        registry.counter(
            "tax_risk_external_fetch_total",
            "External financial data fetch outcomes.",
            ("source", "provenance", "result"),
        )
        registry.counter(
            "tax_risk_external_fetch_retry_total",
            "External financial data fetch retries.",
            ("source", "error_code"),
        )
        registry.counter(
            "tax_risk_external_fetch_failure_total",
            "External financial data fetch failures.",
            ("source", "error_code"),
        )
        registry.histogram(
            "tax_risk_external_fetch_duration_seconds",
            "External financial data fetch duration including cache coordination.",
            ("source", "provenance"),
            buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 300),
        )

    def success(self, outcome: FetchOutcome, duration_seconds: float) -> None:
        self._registry.metric("tax_risk_external_fetch_total").inc(
            {
                "source": outcome.source_name,
                "provenance": outcome.provenance.value,
                "result": "NO_DATA" if outcome.record_count == 0 else "SUCCESS",
            }
        )
        self._registry.metric("tax_risk_external_fetch_duration_seconds").observe(
            {
                "source": outcome.source_name,
                "provenance": outcome.provenance.value,
            },
            duration_seconds,
        )

    def retry(
        self,
        request: FetchRequest,
        error: Exception,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        del attempt, delay_seconds
        self._registry.metric("tax_risk_external_fetch_retry_total").inc(
            {"source": request.source_name, "error_code": _error_code(error)}
        )

    def failure(
        self,
        request: FetchRequest,
        error: Exception,
        duration_seconds: float,
    ) -> None:
        del duration_seconds
        self._registry.metric("tax_risk_external_fetch_failure_total").inc(
            {"source": request.source_name, "error_code": _error_code(error)}
        )


def build_external_fetch_coordinator(
    settings: Settings,
    sources: Mapping[str, DgcFetchSource],
    *,
    cache: FetchCache | None = None,
    observer: FetchObserver | None = None,
) -> ParallelFetchCoordinator:
    resolved_cache: FetchCache
    if cache is None:
        if settings.external_fetch_cache_enabled:
            resolved_cache = cast(
                FetchCache,
                RedisFetchCache.from_url(
                    settings.redis_url,
                    prefix=settings.external_fetch_cache_prefix,
                ),
            )
        else:
            resolved_cache = NullFetchCache()
    else:
        resolved_cache = cache
    return ParallelFetchCoordinator(
        sources,
        resolved_cache,
        FetchCoordinatorConfig(
            max_workers=settings.external_fetch_max_workers,
            source_concurrency=settings.external_fetch_source_concurrency,
            cache_ttl_seconds=settings.external_fetch_cache_ttl_seconds,
            empty_cache_ttl_seconds=settings.external_fetch_empty_cache_ttl_seconds,
            lock_ttl_seconds=settings.external_fetch_lock_ttl_seconds,
            lock_wait_seconds=settings.external_fetch_lock_wait_seconds,
            lock_poll_seconds=settings.external_fetch_lock_poll_seconds,
            retry_max_attempts=settings.external_fetch_retry_max_attempts,
            retry_base_delay_seconds=(
                settings.external_fetch_retry_base_delay_seconds
            ),
            retry_max_delay_seconds=settings.external_fetch_retry_max_delay_seconds,
            retry_jitter_ratio=settings.external_fetch_retry_jitter_ratio,
        ),
        observer=observer,
    )


def _error_code(error: Exception) -> str:
    candidate = getattr(error, "error_code", type(error).__name__)
    normalized = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in str(candidate)
    )
    return normalized[:128] or "UNKNOWN"


__all__ = ["MetricFetchObserver", "build_external_fetch_coordinator"]

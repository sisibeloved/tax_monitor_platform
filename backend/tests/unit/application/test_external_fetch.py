from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from threading import Event, Lock
from time import sleep

import pytest

from tax_risk.adapters.cache.memory_fetch_cache import MemoryFetchCache
from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcAuthenticationError,
    DgcCertificateError,
    DgcFetchResult,
    DgcHttpError,
    DgcSchemaError,
    DgcTransportError,
)
from tax_risk.application.external_fetch import (
    ExternalFetchBatchError,
    FetchCoordinatorConfig,
    FetchLockTimeoutError,
    FetchProvenance,
    FetchRequest,
    ParallelFetchCoordinator,
)


NOW = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)


def _result(value: object = "1") -> DgcFetchResult:
    records = ({"value": value},)
    encoded = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: str(item) if isinstance(item, Decimal) else TypeError(),
    ).encode()
    return DgcFetchResult(records=records, checksum=sha256(encoded).hexdigest())


class CountingSource:
    def __init__(self, result: DgcFetchResult) -> None:
        self.result = result
        self.calls = 0
        self._lock = Lock()

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        del parameters
        with self._lock:
            self.calls += 1
        return self.result


def _config(**overrides: object) -> FetchCoordinatorConfig:
    values: dict[str, object] = {
        "max_workers": 4,
        "source_concurrency": {"profit": 2},
        "cache_ttl_seconds": 100,
        "empty_cache_ttl_seconds": 10,
        "lock_ttl_seconds": 30,
        "lock_wait_seconds": 1.0,
        "lock_poll_seconds": 0.001,
        "retry_max_attempts": 3,
        "retry_base_delay_seconds": 0.001,
        "retry_max_delay_seconds": 0.01,
        "retry_jitter_ratio": 0.0,
    }
    values.update(overrides)
    return FetchCoordinatorConfig(**values)  # type: ignore[arg-type]


def test_cache_hit_avoids_second_source_call_and_preserves_decimal() -> None:
    source = CountingSource(_result(Decimal("123.4500")))
    coordinator = ParallelFetchCoordinator(
        {"profit": source}, MemoryFetchCache(), _config(), now=lambda: NOW
    )
    request = FetchRequest("profit", {"company": "3000", "year": 2026})
    try:
        first = coordinator.fetch_one(request)
        second = coordinator.fetch_one(request)
    finally:
        coordinator.close()

    assert source.calls == 1
    assert first.provenance is FetchProvenance.LIVE
    assert first.attempts == 1
    assert second.provenance is FetchProvenance.CACHE
    assert second.attempts == 0
    assert second.result.records[0]["value"] == Decimal("123.4500")


def test_successful_empty_result_uses_short_negative_cache_ttl() -> None:
    empty = DgcFetchResult(
        records=(),
        checksum=sha256(b"[]").hexdigest(),
    )
    cache = MemoryFetchCache()
    source = CountingSource(empty)
    coordinator = ParallelFetchCoordinator(
        {"profit": source}, cache, _config(), now=lambda: NOW
    )
    request = FetchRequest("profit", {"company": "NO_DATA"})
    try:
        assert coordinator.fetch_one(request).record_count == 0
        assert coordinator.fetch_one(request).provenance is FetchProvenance.CACHE
    finally:
        coordinator.close()

    assert source.calls == 1
    assert cache.put_ttls == [10]


def test_concurrent_identical_requests_coalesce_to_one_live_call() -> None:
    entered = Event()
    release = Event()

    class BlockingSource(CountingSource):
        def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
            with self._lock:
                self.calls += 1
            entered.set()
            assert release.wait(timeout=2)
            return self.result

    source = BlockingSource(_result())
    coordinator = ParallelFetchCoordinator(
        {"profit": source}, MemoryFetchCache(), _config(), now=lambda: NOW
    )
    request = FetchRequest("profit", {"company": "3000"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.fetch_one, request)
        assert entered.wait(timeout=1)
        second = pool.submit(coordinator.fetch_one, request)
        sleep(0.02)
        release.set()
        outcomes = (first.result(timeout=2), second.result(timeout=2))
    coordinator.close()

    assert source.calls == 1
    assert {outcome.provenance for outcome in outcomes} == {
        FetchProvenance.LIVE,
        FetchProvenance.CACHE,
    }


@pytest.mark.parametrize(
    ("error", "expected_calls"),
    [
        (DgcTransportError("transport"), 3),
        (DgcHttpError(429, "limited"), 3),
        (DgcHttpError(503, "unavailable"), 3),
        (DgcAuthenticationError("auth"), 1),
        (DgcCertificateError("certificate"), 1),
        (DgcSchemaError("schema"), 1),
        (DgcHttpError(400, "bad request"), 1),
    ],
)
def test_only_transient_failures_are_retried(
    error: Exception,
    expected_calls: int,
) -> None:
    class FailingSource:
        calls = 0

        def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
            del parameters
            self.calls += 1
            raise error

    source = FailingSource()
    coordinator = ParallelFetchCoordinator(
        {"profit": source},
        MemoryFetchCache(),
        _config(),
        sleeper=lambda _delay: None,
    )
    try:
        with pytest.raises(type(error)):
            coordinator.fetch_one(FetchRequest("profit", {"company": "3000"}))
    finally:
        coordinator.close()
    assert source.calls == expected_calls


def test_fetch_many_preserves_input_order_and_enforces_source_limit() -> None:
    lock = Lock()
    active = 0
    peak = 0

    class TrackingSource:
        def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            sleep(0.015)
            with lock:
                active -= 1
            return _result(parameters["position"])

    coordinator = ParallelFetchCoordinator(
        {"profit": TrackingSource()},
        MemoryFetchCache(),
        _config(max_workers=8, source_concurrency={"profit": 2}),
        now=lambda: NOW,
    )
    requests = tuple(
        FetchRequest("profit", {"position": position}, request_id=str(position))
        for position in range(8)
    )
    try:
        outcomes = coordinator.fetch_many(requests)
    finally:
        coordinator.close()

    assert peak == 2
    assert [outcome.request_id for outcome in outcomes] == [str(value) for value in range(8)]
    assert [outcome.result.records[0]["value"] for outcome in outcomes] == list(range(8))


def test_global_worker_limit_applies_across_sources() -> None:
    lock = Lock()
    release = Event()
    all_workers_entered = Event()
    active = 0
    peak = 0

    class TrackingSource:
        def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
            nonlocal active, peak
            del parameters
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 3:
                    all_workers_entered.set()
            assert release.wait(timeout=2)
            with lock:
                active -= 1
            return _result()

    sources = {"source_a": TrackingSource(), "source_b": TrackingSource()}
    coordinator = ParallelFetchCoordinator(
        sources,
        MemoryFetchCache(),
        _config(
            max_workers=3,
            source_concurrency={"source_a": 3, "source_b": 3},
        ),
    )
    requests = tuple(
        FetchRequest(
            "source_a" if position % 2 == 0 else "source_b",
            {"position": position},
        )
        for position in range(8)
    )
    with ThreadPoolExecutor(max_workers=1) as caller:
        batch = caller.submit(coordinator.fetch_many, requests)
        assert all_workers_entered.wait(timeout=1)
        assert peak == 3
        release.set()
        assert len(batch.result(timeout=3)) == 8
    coordinator.close()


def test_batch_failure_is_closed_and_never_replaced_with_cache_or_mock() -> None:
    class Source:
        def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
            if parameters["company"] == "bad":
                raise DgcAuthenticationError("auth")
            return _result()

    coordinator = ParallelFetchCoordinator(
        {"profit": Source()}, MemoryFetchCache(), _config()
    )
    requests = (
        FetchRequest("profit", {"company": "good"}, request_id="good"),
        FetchRequest("profit", {"company": "bad"}, request_id="bad"),
    )
    try:
        with pytest.raises(ExternalFetchBatchError) as captured:
            coordinator.fetch_many(requests)
    finally:
        coordinator.close()
    assert captured.value.request_id == "bad"
    assert isinstance(captured.value.__cause__, DgcAuthenticationError)


def test_waiter_lock_timeout_fails_without_calling_source() -> None:
    cache = MemoryFetchCache()
    request = FetchRequest("profit", {"company": "3000"})
    assert cache.try_acquire_lock(request, "other-owner", ttl_seconds=30)
    source = CountingSource(_result())
    coordinator = ParallelFetchCoordinator(
        {"profit": source},
        cache,
        _config(lock_wait_seconds=0.01, lock_poll_seconds=0.001),
    )
    try:
        with pytest.raises(FetchLockTimeoutError):
            coordinator.fetch_one(request)
    finally:
        coordinator.close()
    assert source.calls == 0


def test_request_repr_hides_business_parameters_and_rejects_credentials() -> None:
    request = FetchRequest("profit", {"company": "SECRET-COMPANY"})
    assert "SECRET-COMPANY" not in repr(request)
    with pytest.raises(ValueError, match="credentials"):
        FetchRequest("profit", {"app_secret": "must-not-enter-request"})

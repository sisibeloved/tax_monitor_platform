"""Bounded, cache-backed coordination for external financial data reads."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import random
import re
from secrets import token_urlsafe
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic, sleep
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcCertificateError,
    DgcFetchResult,
    DgcHttpError,
    DgcTransportError,
)


_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SENSITIVE_PARAMETER_PARTS = (
    "appkey",
    "appsecret",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


class ExternalFetchError(RuntimeError):
    """Base class for stable, non-secret-bearing fetch coordination failures."""


class UnknownFetchSourceError(ExternalFetchError):
    pass


class FetchCoordinatorClosedError(ExternalFetchError):
    pass


class FetchLockTimeoutError(ExternalFetchError):
    pass


class FetchLockLostError(ExternalFetchError):
    pass


class ExternalFetchBatchError(ExternalFetchError):
    """A batch-level error which intentionally excludes request parameters."""

    def __init__(self, request_id: str, source_name: str) -> None:
        super().__init__(
            f"external fetch batch failed for request {request_id} from {source_name}"
        )
        self.request_id = request_id
        self.source_name = source_name


class FetchProvenance(StrEnum):
    LIVE = "LIVE"
    CACHE = "CACHE"


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """One external read. Parameters are hidden to prevent accidental disclosure."""

    source_name: str
    parameters: Mapping[str, object] = field(repr=False)
    schema_version: str = "1"
    request_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        source_name = self.source_name.strip()
        if not _SOURCE_PATTERN.fullmatch(source_name):
            raise ValueError("source_name must be a stable non-secret identifier")
        schema_version = self.schema_version.strip()
        if not schema_version or len(schema_version) > 64:
            raise ValueError("schema_version must be between 1 and 64 characters")
        request_id = self.request_id.strip()
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must be between 1 and 128 characters")
        if not isinstance(self.parameters, Mapping) or any(
            not isinstance(key, str) for key in self.parameters
        ):
            raise TypeError("parameters must be a mapping with string keys")
        sensitive_keys = sorted(
            key
            for key in self.parameters
            if any(part in _compact_key(key) for part in _SENSITIVE_PARAMETER_PARTS)
        )
        if sensitive_keys:
            raise ValueError("credentials must not be included in fetch parameters")
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class CachedFetch:
    result: DgcFetchResult = field(repr=False)
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    request_id: str
    source_name: str
    provenance: FetchProvenance
    attempts: int
    fetched_at: datetime
    record_count: int
    checksum: str
    result: DgcFetchResult = field(repr=False)


class DgcFetchSource(Protocol):
    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult: ...


class FetchCache(Protocol):
    @property
    def enabled(self) -> bool: ...

    def get(self, request: FetchRequest) -> CachedFetch | None: ...

    def put(
        self,
        request: FetchRequest,
        value: CachedFetch,
        *,
        ttl_seconds: int,
    ) -> None: ...

    def try_acquire_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    def refresh_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    def release_lock(self, request: FetchRequest, owner: str) -> None: ...

    def close(self) -> None: ...


class FetchObserver(Protocol):
    def success(self, outcome: FetchOutcome, duration_seconds: float) -> None: ...

    def retry(
        self,
        request: FetchRequest,
        error: Exception,
        attempt: int,
        delay_seconds: float,
    ) -> None: ...

    def failure(
        self,
        request: FetchRequest,
        error: Exception,
        duration_seconds: float,
    ) -> None: ...


class NullFetchObserver:
    def success(self, outcome: FetchOutcome, duration_seconds: float) -> None:
        del outcome, duration_seconds

    def retry(
        self,
        request: FetchRequest,
        error: Exception,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        del request, error, attempt, delay_seconds

    def failure(
        self,
        request: FetchRequest,
        error: Exception,
        duration_seconds: float,
    ) -> None:
        del request, error, duration_seconds


class NullFetchCache:
    """Explicit no-cache adapter for local development and isolated tests."""

    enabled = False

    def get(self, request: FetchRequest) -> CachedFetch | None:
        del request
        return None

    def put(
        self,
        request: FetchRequest,
        value: CachedFetch,
        *,
        ttl_seconds: int,
    ) -> None:
        del request, value, ttl_seconds

    def try_acquire_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        del request, owner, ttl_seconds
        return True

    def refresh_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        del request, owner, ttl_seconds
        return True

    def release_lock(self, request: FetchRequest, owner: str) -> None:
        del request, owner

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class FetchCoordinatorConfig:
    max_workers: int = 12
    source_concurrency: Mapping[str, int] = field(default_factory=dict)
    cache_ttl_seconds: int = 900
    empty_cache_ttl_seconds: int = 60
    lock_ttl_seconds: int = 300
    lock_wait_seconds: float = 305.0
    lock_poll_seconds: float = 0.1
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.25
    retry_max_delay_seconds: float = 5.0
    retry_jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        positive_ints = {
            "max_workers": self.max_workers,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "empty_cache_ttl_seconds": self.empty_cache_ttl_seconds,
            "lock_ttl_seconds": self.lock_ttl_seconds,
            "retry_max_attempts": self.retry_max_attempts,
        }
        if any(type(value) is not int or value <= 0 for value in positive_ints.values()):
            raise ValueError("fetch integer limits must be positive integers")
        positive_floats = {
            "lock_wait_seconds": self.lock_wait_seconds,
            "lock_poll_seconds": self.lock_poll_seconds,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
            for value in positive_floats.values()
        ):
            raise ValueError("fetch timing limits must be positive")
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError("retry maximum delay must be at least the base delay")
        if not 0 <= self.retry_jitter_ratio <= 1:
            raise ValueError("retry_jitter_ratio must be between 0 and 1")
        normalized_limits: dict[str, int] = {}
        for source_name, limit in self.source_concurrency.items():
            if not _SOURCE_PATTERN.fullmatch(source_name):
                raise ValueError("source concurrency keys must be stable identifiers")
            if type(limit) is not int or limit <= 0:
                raise ValueError("source concurrency limits must be positive integers")
            normalized_limits[source_name] = limit
        object.__setattr__(self, "source_concurrency", MappingProxyType(normalized_limits))


class ParallelFetchCoordinator:
    """Runs independent reads concurrently while coalescing identical cache misses."""

    def __init__(
        self,
        sources: Mapping[str, DgcFetchSource],
        cache: FetchCache,
        config: FetchCoordinatorConfig,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
        random_value: Callable[[], float] = random.random,
        observer: FetchObserver | None = None,
    ) -> None:
        if not sources:
            raise ValueError("at least one external fetch source is required")
        normalized_sources: dict[str, DgcFetchSource] = {}
        for source_name, source in sources.items():
            if not _SOURCE_PATTERN.fullmatch(source_name):
                raise ValueError("source names must be stable non-secret identifiers")
            normalized_sources[source_name] = source
        self._sources = MappingProxyType(normalized_sources)
        self._cache = cache
        self._config = config
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock
        self._sleep = sleeper
        self._random = random_value
        self._observer = observer or NullFetchObserver()
        self._source_semaphores = {
            source_name: BoundedSemaphore(
                config.source_concurrency.get(source_name, config.max_workers)
            )
            for source_name in normalized_sources
        }
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix="external-fetch",
        )
        self._state_lock = Lock()
        self._closed = False

    def fetch_one(self, request: FetchRequest) -> FetchOutcome:
        self._ensure_open()
        return self._execute(request)

    def fetch_many(self, requests: Sequence[FetchRequest]) -> tuple[FetchOutcome, ...]:
        self._ensure_open()
        submitted: list[tuple[FetchRequest, Future[FetchOutcome]]] = [
            (request, self._executor.submit(self._execute, request)) for request in requests
        ]
        outcomes: list[FetchOutcome] = []
        failures: list[tuple[FetchRequest, Exception]] = []
        for request, future in submitted:
            try:
                outcomes.append(future.result())
            except Exception as caught:
                failures.append((request, caught))
        if failures:
            failed_request, cause = failures[0]
            raise ExternalFetchBatchError(
                failed_request.request_id,
                failed_request.source_name,
            ) from cause
        return tuple(outcomes)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._cache.close()

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise FetchCoordinatorClosedError("external fetch coordinator is closed")

    def _execute(self, request: FetchRequest) -> FetchOutcome:
        started_at = self._monotonic()
        try:
            outcome = self._execute_observed(request)
        except Exception as error:
            self._observer.failure(
                request,
                error,
                max(0.0, self._monotonic() - started_at),
            )
            raise
        self._observer.success(
            outcome,
            max(0.0, self._monotonic() - started_at),
        )
        return outcome

    def _execute_observed(self, request: FetchRequest) -> FetchOutcome:
        source = self._sources.get(request.source_name)
        if source is None:
            raise UnknownFetchSourceError(
                f"external fetch source {request.source_name!r} is not registered"
            )
        cached = self._cache.get(request)
        if cached is not None:
            return _outcome(request, cached, FetchProvenance.CACHE, attempts=0)
        if not self._cache.enabled:
            value, attempts = self._fetch_live(request, source)
            return _outcome(request, value, FetchProvenance.LIVE, attempts=attempts)

        owner = token_urlsafe(24)
        if not self._cache.try_acquire_lock(
            request,
            owner,
            ttl_seconds=self._config.lock_ttl_seconds,
        ):
            return self._wait_for_owner(request)

        stop_heartbeat = Event()
        lock_lost = Event()
        heartbeat = Thread(
            target=self._heartbeat_lock,
            args=(request, owner, stop_heartbeat, lock_lost),
            name="external-fetch-lock-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            cached = self._cache.get(request)
            if cached is not None:
                return _outcome(request, cached, FetchProvenance.CACHE, attempts=0)
            value, attempts = self._fetch_live(request, source)
            if lock_lost.is_set():
                raise FetchLockLostError(
                    f"cache lock was lost for request {request.request_id}"
                )
            ttl_seconds = (
                self._config.empty_cache_ttl_seconds
                if not value.result.records
                else self._config.cache_ttl_seconds
            )
            self._cache.put(request, value, ttl_seconds=ttl_seconds)
            return _outcome(request, value, FetchProvenance.LIVE, attempts=attempts)
        finally:
            stop_heartbeat.set()
            heartbeat.join()
            self._cache.release_lock(request, owner)

    def _fetch_live(
        self,
        request: FetchRequest,
        source: DgcFetchSource,
    ) -> tuple[CachedFetch, int]:
        semaphore = self._source_semaphores[request.source_name]
        with semaphore:
            for attempt in range(1, self._config.retry_max_attempts + 1):
                try:
                    result = source.fetch(request.parameters)
                    return CachedFetch(result=result, fetched_at=self._now()), attempt
                except Exception as error:
                    if attempt >= self._config.retry_max_attempts or not _retryable(error):
                        raise
                    delay = self._retry_delay(attempt)
                    self._observer.retry(request, error, attempt, delay)
                    self._sleep(delay)
        raise AssertionError("unreachable retry loop")

    def _retry_delay(self, failed_attempt: int) -> float:
        base = min(
            self._config.retry_max_delay_seconds,
            self._config.retry_base_delay_seconds * (2 ** (failed_attempt - 1)),
        )
        jitter = base * self._config.retry_jitter_ratio * (2 * self._random() - 1)
        return float(max(0.0, base + jitter))

    def _wait_for_owner(self, request: FetchRequest) -> FetchOutcome:
        deadline = self._monotonic() + self._config.lock_wait_seconds
        while True:
            cached = self._cache.get(request)
            if cached is not None:
                return _outcome(request, cached, FetchProvenance.CACHE, attempts=0)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise FetchLockTimeoutError(
                    f"timed out waiting for cache owner for request {request.request_id}"
                )
            self._sleep(min(self._config.lock_poll_seconds, remaining))

    def _heartbeat_lock(
        self,
        request: FetchRequest,
        owner: str,
        stop: Event,
        lock_lost: Event,
    ) -> None:
        interval = max(0.1, self._config.lock_ttl_seconds / 3)
        while not stop.wait(interval):
            try:
                refreshed = self._cache.refresh_lock(
                    request,
                    owner,
                    ttl_seconds=self._config.lock_ttl_seconds,
                )
            except Exception:
                lock_lost.set()
                return
            if not refreshed:
                lock_lost.set()
                return


class CoordinatedDgcSource:
    """Compatibility adapter for services which currently call ``source.fetch``."""

    def __init__(
        self,
        coordinator: ParallelFetchCoordinator,
        source_name: str,
        *,
        schema_version: str = "1",
    ) -> None:
        self._coordinator = coordinator
        self._source_name = source_name
        self._schema_version = schema_version

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        return self._coordinator.fetch_one(
            FetchRequest(
                source_name=self._source_name,
                parameters=parameters,
                schema_version=self._schema_version,
            )
        ).result


def _retryable(error: Exception) -> bool:
    if isinstance(error, DgcCertificateError):
        return False
    if isinstance(error, DgcTransportError):
        return True
    if isinstance(error, DgcHttpError):
        return error.status_code in {408, 429} or error.status_code >= 500
    return False


def _outcome(
    request: FetchRequest,
    value: CachedFetch,
    provenance: FetchProvenance,
    *,
    attempts: int,
) -> FetchOutcome:
    return FetchOutcome(
        request_id=request.request_id,
        source_name=request.source_name,
        provenance=provenance,
        attempts=attempts,
        fetched_at=value.fetched_at,
        record_count=len(value.result.records),
        checksum=value.result.checksum,
        result=value.result,
    )


def _compact_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


__all__ = [
    "CachedFetch",
    "CoordinatedDgcSource",
    "DgcFetchSource",
    "ExternalFetchBatchError",
    "ExternalFetchError",
    "FetchCache",
    "FetchCoordinatorClosedError",
    "FetchCoordinatorConfig",
    "FetchLockLostError",
    "FetchLockTimeoutError",
    "FetchOutcome",
    "FetchObserver",
    "FetchProvenance",
    "FetchRequest",
    "NullFetchCache",
    "NullFetchObserver",
    "ParallelFetchCoordinator",
    "UnknownFetchSourceError",
]

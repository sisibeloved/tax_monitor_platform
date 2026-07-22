"""Thread-safe deterministic cache used by unit tests and local composition."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable

from tax_risk.adapters.cache.redis_fetch_cache import _request_digest
from tax_risk.application.external_fetch import CachedFetch, FetchRequest


@dataclass(slots=True)
class _Value:
    payload: CachedFetch
    expires_at: float


@dataclass(slots=True)
class _Lease:
    owner: str
    expires_at: float


class MemoryFetchCache:
    enabled = True

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._values: dict[str, _Value] = {}
        self._locks: dict[str, _Lease] = {}
        self._mutex = Lock()
        self.put_ttls: list[int] = []

    def get(self, request: FetchRequest) -> CachedFetch | None:
        key = self._key(request)
        with self._mutex:
            value = self._values.get(key)
            if value is None:
                return None
            if value.expires_at <= self._clock():
                del self._values[key]
                return None
            return value.payload

    def put(
        self,
        request: FetchRequest,
        value: CachedFetch,
        *,
        ttl_seconds: int,
    ) -> None:
        with self._mutex:
            self._values[self._key(request)] = _Value(
                payload=value,
                expires_at=self._clock() + ttl_seconds,
            )
            self.put_ttls.append(ttl_seconds)

    def try_acquire_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        key = self._key(request)
        with self._mutex:
            existing = self._locks.get(key)
            if existing is not None and existing.expires_at > self._clock():
                return False
            self._locks[key] = _Lease(owner, self._clock() + ttl_seconds)
            return True

    def refresh_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        key = self._key(request)
        with self._mutex:
            existing = self._locks.get(key)
            if (
                existing is None
                or existing.owner != owner
                or existing.expires_at <= self._clock()
            ):
                return False
            self._locks[key] = _Lease(owner, self._clock() + ttl_seconds)
            return True

    def release_lock(self, request: FetchRequest, owner: str) -> None:
        key = self._key(request)
        with self._mutex:
            existing = self._locks.get(key)
            if existing is not None and existing.owner == owner:
                del self._locks[key]

    def close(self) -> None:
        return None

    @staticmethod
    def _key(request: FetchRequest) -> str:
        return f"{request.source_name}:{_request_digest(request)}"


__all__ = ["MemoryFetchCache"]

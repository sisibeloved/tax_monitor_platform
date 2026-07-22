from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import cast

import pytest

from tax_risk.adapters.cache.redis_fetch_cache import (
    FetchCacheCorruptionError,
    RedisClient,
    RedisFetchCache,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.external_fetch import CachedFetch, FetchRequest


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes | str] = {}
        self.expiries: dict[str, int] = {}
        self.closed = False

    def get(self, name: str) -> bytes | str | None:
        return self.values.get(name)

    def set(
        self,
        name: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object:
        if nx and name in self.values:
            return False
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = ex
        return True

    def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                deleted += 1
                del self.values[name]
        return deleted

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert numkeys == 1
        key = cast(str, keys_and_args[0])
        owner = cast(str, keys_and_args[1])
        if self.values.get(key) != owner:
            return 0
        if "DEL" in script:
            del self.values[key]
            return 1
        self.expiries[key] = cast(int, keys_and_args[2])
        return 1

    def close(self) -> None:
        self.closed = True


def _result() -> DgcFetchResult:
    records = ({"amount": Decimal("10.2300"), "period": 6},)
    encoded = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: str(value) if isinstance(value, Decimal) else TypeError(),
    ).encode()
    return DgcFetchResult(records=records, checksum=sha256(encoded).hexdigest())


def test_key_is_stable_by_parameter_order_and_contains_no_raw_parameters() -> None:
    cache = RedisFetchCache(cast(RedisClient, FakeRedis()), prefix="test:fetch")
    first = FetchRequest("profit", {"company": "3000", "year": 2026})
    second = FetchRequest("profit", {"year": 2026, "company": "3000"})

    first_key = cache.cache_key_for_testing(first)
    assert first_key == cache.cache_key_for_testing(second)
    assert "3000" not in first_key
    assert "2026" not in first_key


def test_round_trip_preserves_decimal_and_validates_source_checksum() -> None:
    client = FakeRedis()
    cache = RedisFetchCache(cast(RedisClient, client), prefix="test:fetch")
    request = FetchRequest("profit", {"company": "3000"})
    fetched_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    cache.put(
        request,
        CachedFetch(result=_result(), fetched_at=fetched_at),
        ttl_seconds=120,
    )

    cached = cache.get(request)
    assert cached is not None
    assert cached.fetched_at == fetched_at
    assert cached.result.records[0]["amount"] == Decimal("10.2300")
    assert client.expiries[cache.cache_key_for_testing(request)] == 120


def test_corrupt_envelope_is_deleted_and_fails_closed() -> None:
    client = FakeRedis()
    cache = RedisFetchCache(cast(RedisClient, client), prefix="test:fetch")
    request = FetchRequest("profit", {"company": "3000"})
    key = cache.cache_key_for_testing(request)
    client.values[key] = b'{"version":1,"tampered":true}'

    with pytest.raises(FetchCacheCorruptionError):
        cache.get(request)
    assert key not in client.values


def test_lock_release_and_refresh_compare_owner_atomically() -> None:
    client = FakeRedis()
    cache = RedisFetchCache(cast(RedisClient, client), prefix="test:fetch")
    request = FetchRequest("profit", {"company": "3000"})

    assert cache.try_acquire_lock(request, "owner-1", ttl_seconds=30)
    assert not cache.try_acquire_lock(request, "owner-2", ttl_seconds=30)
    assert not cache.refresh_lock(request, "owner-2", ttl_seconds=60)
    assert cache.refresh_lock(request, "owner-1", ttl_seconds=60)
    cache.release_lock(request, "owner-2")
    assert not cache.try_acquire_lock(request, "owner-2", ttl_seconds=30)
    cache.release_lock(request, "owner-1")
    assert cache.try_acquire_lock(request, "owner-2", ttl_seconds=30)

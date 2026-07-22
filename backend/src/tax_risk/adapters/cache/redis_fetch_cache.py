"""Redis cache for validated DGC fetch results with distributed lock leases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import re
from typing import Protocol, cast

from redis import Redis

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.external_fetch import CachedFetch, FetchRequest


_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""".strip()
_REFRESH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""".strip()


class FetchCacheError(RuntimeError):
    pass


class FetchCacheCorruptionError(FetchCacheError):
    pass


class RedisClient(Protocol):
    def get(self, name: str) -> bytes | str | None: ...

    def set(
        self,
        name: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object: ...

    def delete(self, *names: str) -> int: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RedisFetchCache:
    client: RedisClient
    prefix: str = "tax-risk:external-fetch"
    enabled: bool = True

    def __post_init__(self) -> None:
        normalized = self.prefix.strip().rstrip(":")
        if not _PREFIX_PATTERN.fullmatch(normalized):
            raise ValueError("cache prefix contains unsupported characters")
        object.__setattr__(self, "prefix", normalized)

    @classmethod
    def from_url(cls, redis_url: str, *, prefix: str) -> RedisFetchCache:
        client = Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
        return cls(cast(RedisClient, client), prefix=prefix)

    def get(self, request: FetchRequest) -> CachedFetch | None:
        key = self._cache_key(request)
        raw = self.client.get(key)
        if raw is None:
            return None
        try:
            return _decode_envelope(raw, expected_source=request.source_name)
        except Exception as error:
            self.client.delete(key)
            if isinstance(error, FetchCacheCorruptionError):
                raise
            raise FetchCacheCorruptionError("external fetch cache entry is invalid") from error

    def put(
        self,
        request: FetchRequest,
        value: CachedFetch,
        *,
        ttl_seconds: int,
    ) -> None:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("cache TTL must be a positive integer")
        self.client.set(
            self._cache_key(request),
            _encode_envelope(request.source_name, value),
            ex=ttl_seconds,
        )

    def try_acquire_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        return bool(
            self.client.set(
                self._lock_key(request),
                owner,
                ex=ttl_seconds,
                nx=True,
            )
        )

    def refresh_lock(
        self,
        request: FetchRequest,
        owner: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        result = self.client.eval(
            _REFRESH_SCRIPT,
            1,
            self._lock_key(request),
            owner,
            ttl_seconds,
        )
        return bool(result)

    def release_lock(self, request: FetchRequest, owner: str) -> None:
        self.client.eval(_RELEASE_SCRIPT, 1, self._lock_key(request), owner)

    def close(self) -> None:
        self.client.close()

    def cache_key_for_testing(self, request: FetchRequest) -> str:
        """Expose the opaque key for deterministic contract tests only."""

        return self._cache_key(request)

    def _cache_key(self, request: FetchRequest) -> str:
        return f"{self.prefix}:v1:{request.source_name}:{_request_digest(request)}"

    def _lock_key(self, request: FetchRequest) -> str:
        return f"{self._cache_key(request)}:lock"


def _request_digest(request: FetchRequest) -> str:
    canonical = {
        "schema_version": request.schema_version,
        "parameters": _encode_value(request.parameters),
    }
    return sha256(_canonical_json(canonical)).hexdigest()


def _encode_envelope(source_name: str, value: CachedFetch) -> bytes:
    payload = {
        "records": _encode_value(value.result.records),
        "source_checksum": value.result.checksum,
    }
    payload_bytes = _canonical_json(payload)
    envelope = {
        "version": 1,
        "source": source_name,
        "fetched_at": value.fetched_at.isoformat(),
        "payload_sha256": sha256(payload_bytes).hexdigest(),
        "payload": payload,
    }
    return _canonical_json(envelope)


def _decode_envelope(raw: bytes | str, *, expected_source: str) -> CachedFetch:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise FetchCacheCorruptionError("external fetch cache is not valid JSON") from error
    if not isinstance(decoded, dict) or decoded.get("version") != 1:
        raise FetchCacheCorruptionError("external fetch cache version is unsupported")
    if decoded.get("source") != expected_source:
        raise FetchCacheCorruptionError("external fetch cache source does not match")
    payload = decoded.get("payload")
    payload_sha256 = decoded.get("payload_sha256")
    if not isinstance(payload, dict) or not isinstance(payload_sha256, str):
        raise FetchCacheCorruptionError("external fetch cache envelope is incomplete")
    if sha256(_canonical_json(payload)).hexdigest() != payload_sha256:
        raise FetchCacheCorruptionError("external fetch cache payload checksum does not match")
    source_checksum = payload.get("source_checksum")
    records_node = payload.get("records")
    if not isinstance(source_checksum, str):
        raise FetchCacheCorruptionError("external fetch cache source checksum is invalid")
    records = _decode_value(records_node)
    if not isinstance(records, tuple) or any(not isinstance(record, dict) for record in records):
        raise FetchCacheCorruptionError("external fetch cache records are invalid")
    typed_records = tuple(cast(Mapping[str, object], record) for record in records)
    if _dgc_checksum(typed_records) != source_checksum:
        raise FetchCacheCorruptionError("external fetch source checksum does not match")
    fetched_at_raw = decoded.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        raise FetchCacheCorruptionError("external fetch timestamp is invalid")
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError as error:
        raise FetchCacheCorruptionError("external fetch timestamp is invalid") from error
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise FetchCacheCorruptionError("external fetch timestamp must include an offset")
    return CachedFetch(
        result=DgcFetchResult(records=typed_records, checksum=source_checksum),
        fetched_at=fetched_at,
    )


def _encode_value(value: object) -> object:
    if value is None:
        return {"$type": "none"}
    if isinstance(value, bool):
        return {"$type": "bool", "value": value}
    if isinstance(value, int):
        return {"$type": "int", "value": str(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("non-finite Decimal values cannot be cached")
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non-finite float values cannot be cached")
        return {"$type": "float", "value": repr(value)}
    if isinstance(value, str):
        return {"$type": "str", "value": value}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("datetime cache values must include an offset")
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("cache mappings must use string keys")
        return {
            "$type": "mapping",
            "value": [[key, _encode_value(value[key])] for key in sorted(value)],
        }
    if isinstance(value, tuple):
        return {"$type": "tuple", "value": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return {"$type": "list", "value": [_encode_value(item) for item in value]}
    raise TypeError(f"unsupported cache value {type(value).__name__}")


def _decode_value(node: object) -> object:
    if not isinstance(node, dict) or set(node) - {"$type", "value"}:
        raise FetchCacheCorruptionError("external fetch cache value is invalid")
    kind = node.get("$type")
    value = node.get("value")
    if kind == "none" and set(node) == {"$type"}:
        return None
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "int" and isinstance(value, str):
        return int(value)
    if kind == "decimal" and isinstance(value, str):
        decoded = Decimal(value)
        if decoded.is_finite():
            return decoded
    if kind == "float" and isinstance(value, str):
        decoded_float = float(value)
        if math.isfinite(decoded_float):
            return decoded_float
    if kind == "str" and isinstance(value, str):
        return value
    if kind == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if kind == "datetime" and isinstance(value, str):
        decoded_datetime = datetime.fromisoformat(value)
        if decoded_datetime.tzinfo is not None and decoded_datetime.utcoffset() is not None:
            return decoded_datetime
    if kind == "mapping" and isinstance(value, list):
        result: dict[str, object] = {}
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or pair[0] in result
            ):
                raise FetchCacheCorruptionError("external fetch cache mapping is invalid")
            result[pair[0]] = _decode_value(pair[1])
        return result
    if kind in {"tuple", "list"} and isinstance(value, list):
        decoded_items = tuple(_decode_value(item) for item in value)
        return decoded_items if kind == "tuple" else list(decoded_items)
    raise FetchCacheCorruptionError("external fetch cache value type is invalid")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _dgc_checksum(records: tuple[Mapping[str, object], ...]) -> str:
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_dgc_json_default,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _dgc_json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported DGC cache value {type(value).__name__}")


__all__ = [
    "FetchCacheCorruptionError",
    "FetchCacheError",
    "RedisClient",
    "RedisFetchCache",
]

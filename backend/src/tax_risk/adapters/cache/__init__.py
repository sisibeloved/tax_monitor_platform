"""External fetch cache adapters."""

from tax_risk.adapters.cache.memory_fetch_cache import MemoryFetchCache
from tax_risk.adapters.cache.redis_fetch_cache import RedisFetchCache

__all__ = ["MemoryFetchCache", "RedisFetchCache"]

"""Mirrors backend/src/lib/server-cache.ts. Server-side in-memory cache
shared across requests hitting the same warm process — eliminates duplicate
AI calls within that window. Bounded by both entry count and approximate
total byte size, evicting least-recently-used entries once either is hit."""
import asyncio
import json
import time
from typing import Awaitable, Callable, TypeVar

MAX_ENTRIES = 500
MAX_TOTAL_BYTES = 25 * 1024 * 1024  # 25 MB

T = TypeVar("T")


def _approx_size(value) -> int:
    try:
        return len(json.dumps(value))
    except Exception:
        return 0


class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}  # key -> {value, expires_at, size} — dict preserves insertion order
        self._total_bytes = 0

    def get(self, key: str):
        item = self._store.get(key)
        if item is None:
            return None
        if time.time() > item["expires_at"]:
            self._delete(key)
            return None
        # Refresh recency: pop + re-insert moves it to the end of iteration order.
        self._store.pop(key)
        self._store[key] = item
        return item["value"]

    def set(self, key: str, value, ttl_seconds: float) -> None:
        existing = self._store.get(key)
        if existing:
            self._total_bytes -= existing["size"]

        size = _approx_size(value)
        self._store[key] = {"value": value, "expires_at": time.time() + ttl_seconds, "size": size}
        self._total_bytes += size

        if len(self._store) > MAX_ENTRIES or self._total_bytes > MAX_TOTAL_BYTES:
            self._evict()

    def _delete(self, key: str) -> None:
        item = self._store.get(key)
        if not item:
            return
        self._total_bytes -= item["size"]
        del self._store[key]

    def _evict(self) -> None:
        now = time.time()
        for k, v in list(self._store.items()):
            if now > v["expires_at"]:
                self._delete(k)

        for k in list(self._store.keys()):
            if len(self._store) <= MAX_ENTRIES and self._total_bytes <= MAX_TOTAL_BYTES:
                break
            self._delete(k)


server_cache = _TTLCache()


def ck(*parts) -> str:
    """Build a stable string key from any serialisable values."""
    return "|".join("" if p is None else str(p) for p in parts)


# In-flight deduplication: if two requests arrive for the same cache key while
# the first is still computing, the second awaits the same task instead of
# spawning a duplicate AI call.
_in_flight: dict[str, asyncio.Task] = {}


async def with_cache(key: str, ttl_seconds: float, compute: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
    """Wrap an async computation with the server cache. Returns (value, from_cache)."""
    hit = server_cache.get(key)
    if hit is not None:
        return hit, True

    existing = _in_flight.get(key)
    if existing:
        return await existing, False

    async def _run():
        try:
            value = await compute()
            server_cache.set(key, value, ttl_seconds)
            return value
        finally:
            _in_flight.pop(key, None)

    task = asyncio.ensure_future(_run())
    _in_flight[key] = task
    return await task, False

"""Upstash Redis rate limiting with in-memory fallback — mirrors
backend/src/lib/rate-limit.ts. Falls back to in-memory automatically when
UPSTASH_REDIS_REST_URL is unset (local dev, or Upstash temporarily unreachable)."""
import os
import time
from typing import Literal

Tier = Literal["standard", "vision", "auth"]

WINDOW_SECONDS = 60 * 60  # 1 hour
_LIMITS: dict[Tier, int] = {"standard": 60, "vision": 20, "auth": 10}

# ─── In-memory fallback ────────────────────────────────────────────────────────
_stores: dict[Tier, dict[str, tuple[int, float]]] = {"standard": {}, "vision": {}, "auth": {}}


def _check_in_memory(ip: str, max_requests: int, store: dict[str, tuple[int, float]]) -> tuple[bool, int]:
    now = time.time()
    entry = store.get(ip)
    if entry is None or now - entry[1] >= WINDOW_SECONDS:
        store[ip] = (1, now)
        return True, max_requests - 1
    count, window_start = entry
    if count >= max_requests:
        return False, 0
    store[ip] = (count + 1, window_start)
    return True, max_requests - (count + 1)


# ─── Upstash limiter (lazily initialised per tier) ────────────────────────────
_limiters: dict[Tier, object] = {}
_limiters_init: dict[Tier, bool] = {}


def _get_limiter(tier: Tier):
    if _limiters_init.get(tier):
        return _limiters.get(tier)
    _limiters_init[tier] = True

    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        _limiters[tier] = None
        return None
    try:
        from upstash_redis import Redis
        from upstash_ratelimit import Ratelimit, SlidingWindow

        limiter = Ratelimit(
            redis=Redis(url=url, token=token),
            limiter=SlidingWindow(max_requests=_LIMITS[tier], window=1, unit="h"),
            prefix=f"eduteach:rl:{tier}",
        )
        _limiters[tier] = limiter
        return limiter
    except Exception as e:  # noqa: BLE001 — mirrors the TS try/catch fallback
        print(f"[rate-limit] Upstash init failed for tier={tier}, using in-memory fallback: {e}")
        _limiters[tier] = None
        return None


def _check(tier: Tier, ip: str) -> tuple[bool, int]:
    limiter = _get_limiter(tier)
    if limiter is None:
        return _check_in_memory(ip, _LIMITS[tier], _stores[tier])
    try:
        result = limiter.limit(ip)
        return result.allowed, result.remaining
    except Exception as e:  # noqa: BLE001
        print(f"[rate-limit] Upstash unavailable, falling back to per-instance memory ({tier}): {e}")
        return _check_in_memory(ip, _LIMITS[tier], _stores[tier])


def check_rate_limit(ip: str) -> tuple[bool, int]:
    """Standard routes: 60 requests per hour per IP."""
    return _check("standard", ip)


def check_vision_rate_limit(ip: str) -> tuple[bool, int]:
    """Vision routes: 20 requests per hour per IP (vision calls are 5-10x more expensive)."""
    return _check("vision", ip)


def check_auth_rate_limit(ip: str) -> tuple[bool, int]:
    """Auth/login/join-code routes: 10 requests per hour per IP."""
    return _check("auth", ip)

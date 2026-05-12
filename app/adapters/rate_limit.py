"""Token Bucket rate limiter — one instance per channel endpoint.

Asyncio-safe: the lock serializes refill and consume so concurrent callers
share the same bucket state correctly.
"""

from __future__ import annotations

import asyncio
from time import monotonic


class TokenBucket:
    """Classic token bucket.

    `rate` = tokens replenished per second.
    `capacity` = maximum tokens held; bursts above this drain immediately.
    """

    __slots__ = ("_capacity", "_lock", "_rate", "_tokens", "_updated")

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        """Block until `cost` tokens are available, then consume them."""
        if cost > self._capacity:
            raise ValueError(f"cost {cost} exceeds capacity {self._capacity}")
        async with self._lock:
            while True:
                now = monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._rate,
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self._rate
                await asyncio.sleep(wait)

"""TokenBucket rate limiter behavior — pure async, no DB."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.adapters import TokenBucket


@pytest.mark.unit
def test_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, capacity=0)


@pytest.mark.unit
async def test_initial_burst_is_immediate() -> None:
    bucket = TokenBucket(rate=1, capacity=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.1


@pytest.mark.unit
async def test_throttles_after_burst() -> None:
    bucket = TokenBucket(rate=10, capacity=2)  # 10 tokens/sec, 2 burst
    await bucket.acquire()
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()  # third call should wait ~0.1s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05


@pytest.mark.unit
async def test_cost_exceeding_capacity_raises() -> None:
    bucket = TokenBucket(rate=1, capacity=2)
    with pytest.raises(ValueError):
        await bucket.acquire(cost=5)


@pytest.mark.unit
async def test_concurrent_acquires_are_serialized() -> None:
    bucket = TokenBucket(rate=100, capacity=1)
    counter = {"n": 0}

    async def take() -> None:
        await bucket.acquire()
        counter["n"] += 1

    await asyncio.gather(*[take() for _ in range(10)])
    assert counter["n"] == 10

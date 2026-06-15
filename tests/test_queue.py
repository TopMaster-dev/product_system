"""TaskQueue abstraction — in-memory backend behavior."""

from __future__ import annotations

import pytest

from app.queue import InMemoryTaskQueue, Task


@pytest.mark.unit
async def test_in_memory_queue_invokes_registered_handler() -> None:
    queue = InMemoryTaskQueue()
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    queue.register("noop", handler)
    await queue.enqueue(Task(name="noop", payload={"foo": "bar"}))

    assert received == [{"foo": "bar"}]


@pytest.mark.unit
async def test_in_memory_queue_silently_skips_unknown_handlers() -> None:
    """Unknown task names log a warning but must not raise."""
    queue = InMemoryTaskQueue()
    await queue.enqueue(Task(name="missing"))

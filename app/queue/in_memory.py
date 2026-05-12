"""In-memory TaskQueue implementation for local dev and tests.

Executes tasks synchronously via registered handlers. Concrete handler
registration happens in Sprint 2 when ingestion pipelines land.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.logging import get_logger
from app.queue.base import Task

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class InMemoryTaskQueue:
    """Synchronous task queue — invokes handlers immediately on enqueue."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    async def enqueue(self, task: Task) -> None:
        handler = self._handlers.get(task.name)
        if handler is None:
            log.warning("task.no_handler", name=task.name)
            return
        log.info("task.run", name=task.name)
        await handler(task.payload)

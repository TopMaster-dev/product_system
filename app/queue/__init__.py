"""Task queue abstraction — Cloud Tasks in prod, in-memory locally.

The `TaskQueue` protocol keeps the application code unaware of the concrete
backend, so the same handlers run identically in tests, local dev, and prod.
A process-wide singleton is maintained so handlers registered at startup
remain visible to webhook handlers served by FastAPI.
"""

from __future__ import annotations

from app.queue.base import Task, TaskQueue
from app.queue.in_memory import InMemoryTaskQueue

__all__ = ["InMemoryTaskQueue", "Task", "TaskQueue", "get_task_queue", "reset_task_queue"]

_queue: TaskQueue | None = None


def get_task_queue() -> TaskQueue:
    """Return the process-wide TaskQueue, creating it on first call.

    The Cloud Tasks backend is wired up in Sprint 4.
    """
    global _queue
    if _queue is not None:
        return _queue

    from app.config import get_settings

    settings = get_settings()
    if settings.task_queue_backend == "in_memory":
        _queue = InMemoryTaskQueue()
        return _queue
    raise NotImplementedError(
        f"Task queue backend {settings.task_queue_backend!r} not yet implemented",
    )


def reset_task_queue() -> None:
    """Test hook — clears the cached singleton."""
    global _queue
    _queue = None

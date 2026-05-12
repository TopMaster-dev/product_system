"""Task queue abstraction — Cloud Tasks in prod, in-memory locally.

The `TaskQueue` protocol keeps the application code unaware of the concrete
backend, so the same handlers run identically in tests, local dev, and prod.
"""

from app.queue.base import Task, TaskQueue
from app.queue.in_memory import InMemoryTaskQueue

__all__ = ["InMemoryTaskQueue", "Task", "TaskQueue", "get_task_queue"]


def get_task_queue() -> TaskQueue:
    """Return the configured TaskQueue backend.

    Cloud Tasks backend is wired up in Sprint 4.
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.task_queue_backend == "in_memory":
        return InMemoryTaskQueue()
    raise NotImplementedError(
        f"Task queue backend {settings.task_queue_backend!r} not yet implemented",
    )

"""TaskQueue protocol and Task definition."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Task(BaseModel):
    """A unit of asynchronous work."""

    name: str  # handler name (e.g. "process_shopify_webhook")
    payload: dict[str, Any] = Field(default_factory=dict)
    scheduled_at: datetime | None = None  # None = run immediately


@runtime_checkable
class TaskQueue(Protocol):
    """Common interface for Cloud Tasks and in-memory backends."""

    async def enqueue(self, task: Task) -> None:
        """Enqueue a task for asynchronous execution."""
        ...

"""Cloud Tasks production backend for the TaskQueue protocol.

Posts each task to a Cloud Tasks queue that targets a Cloud Run handler URL.
The handler decodes the payload and dispatches via the same in-process
handler registry used in tests — keeping the contract identical across
environments.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.logging import get_logger
from app.queue.base import Task

log = get_logger(__name__)


class CloudTasksTaskQueue:
    """Enqueues tasks to a Cloud Tasks queue.

    Lazy-imports `google.cloud.tasks_v2` so the [gcp] extras stay optional.
    """

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        queue_name: str,
        target_url: str,
        service_account_email: str | None = None,
    ) -> None:
        try:
            from google.cloud import tasks_v2  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - extras-gated
            raise RuntimeError(
                "google-cloud-tasks is not installed; install the [gcp] extra"
            ) from exc

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksClient()
        self._parent = self._client.queue_path(project_id, location, queue_name)
        self._target_url = target_url
        self._sa_email = service_account_email

    async def enqueue(self, task: Task) -> None:
        import asyncio

        body = json.dumps({"name": task.name, "payload": task.payload}).encode("utf-8")

        http_request: dict[str, Any] = {
            "http_method": self._tasks_v2.HttpMethod.POST,
            "url": self._target_url,
            "headers": {"Content-Type": "application/json"},
            "body": body,
        }
        if self._sa_email:
            http_request["oidc_token"] = {"service_account_email": self._sa_email}

        cloud_task: dict[str, Any] = {"http_request": http_request}
        if task.scheduled_at is not None:
            cloud_task["schedule_time"] = _to_timestamp(task.scheduled_at)

        def _send() -> None:
            self._client.create_task(parent=self._parent, task=cloud_task)

        await asyncio.to_thread(_send)
        log.info("cloud_tasks.enqueue", name=task.name)


def _to_timestamp(dt: datetime):  # type: ignore[no-untyped-def]
    """Convert a datetime to google.protobuf.Timestamp.

    Imported lazily to avoid the protobuf dependency at module load time.
    """
    from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]

    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts

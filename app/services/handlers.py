"""TaskQueue handlers — register webhook/polling processors.

Phase 1-A keeps handlers thin: they decode the queued payload, build a
NormalizedOrder, and feed it through OrderIngestService inside a single
DB transaction.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters import NormalizedOrder
from app.logging import get_logger
from app.models import WebhookLog, WebhookStatusEnum
from app.queue import InMemoryTaskQueue
from app.services import OrderIngestService

log = get_logger(__name__)

PROCESS_SHOPIFY_WEBHOOK = "process_shopify_webhook"

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def register_handlers(
    queue: InMemoryTaskQueue,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Wire the in-memory queue with concrete async handlers."""
    queue.register(PROCESS_SHOPIFY_WEBHOOK, _make_shopify_webhook_handler(session_factory))


def _make_shopify_webhook_handler(
    session_factory: async_sessionmaker[Any],
) -> Handler:
    async def handler(payload: dict[str, Any]) -> None:
        webhook_log_id = payload["webhook_log_id"]
        order_payload = payload["order"]
        async with session_factory() as session, session.begin():
            try:
                normalized = NormalizedOrder.model_validate(order_payload)
                await OrderIngestService(session).ingest(normalized)
                await session.execute(
                    update(WebhookLog)
                    .where(WebhookLog.id == webhook_log_id)
                    .values(
                        status=WebhookStatusEnum.PROCESSED,
                        processed_at=datetime.now(tz=normalized.ordered_at.tzinfo),
                    )
                )
            except Exception:
                await session.execute(
                    update(WebhookLog)
                    .where(WebhookLog.id == webhook_log_id)
                    .values(status=WebhookStatusEnum.FAILED)
                )
                log.exception("shopify_webhook.handler_failed", id=webhook_log_id)
                raise

    return handler


__all__ = ["PROCESS_SHOPIFY_WEBHOOK", "register_handlers"]


# Convenience: shape of the payload enqueued from the webhook endpoint.
def build_shopify_webhook_payload(
    *, webhook_log_id: int, normalized: NormalizedOrder
) -> dict[str, Any]:
    return {
        "webhook_log_id": webhook_log_id,
        "order": json.loads(normalized.model_dump_json()),
    }

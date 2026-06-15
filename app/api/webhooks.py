"""Webhook receivers — channel-specific HTTP entrypoints.

Shopify contract:
- Verify `X-Shopify-Hmac-Sha256` on the raw request body before doing anything.
  An invalid HMAC returns 401 and is logged; nothing else happens.
- A valid webhook is persisted to `webhook_logs`, enqueued for asynchronous
  processing via the TaskQueue abstraction, and acknowledged with 200 within
  the 5-second Shopify timeout.
- The (channel, webhook_id) UNIQUE constraint makes redelivery idempotent —
  duplicates short-circuit at insert time.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import ShopifyAdapter
from app.config import Settings, get_settings
from app.db import get_session
from app.logging import get_logger
from app.models import WebhookLog, WebhookStatusEnum
from app.queue import Task, TaskQueue, get_task_queue
from app.services.handlers import (
    PROCESS_SHOPIFY_WEBHOOK,
    build_shopify_webhook_payload,
)

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _adapter_from_settings(settings: Settings) -> ShopifyAdapter:
    return ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain or "placeholder.myshopify.com",
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
    )


@router.post(
    "/shopify",
    status_code=status.HTTP_200_OK,
    responses={401: {"description": "Invalid HMAC"}},
)
async def shopify_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    queue: TaskQueue = Depends(get_task_queue),
) -> Response:
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    adapter = _adapter_from_settings(settings)

    webhook_id = headers.get(ShopifyAdapter.HEADER_WEBHOOK_ID, "")
    topic = headers.get(ShopifyAdapter.HEADER_TOPIC, "")
    hmac_valid = adapter.verify_webhook(headers, body)

    if not hmac_valid:
        log.warning("shopify.webhook.hmac_invalid", webhook_id=webhook_id, topic=topic)
        # Best-effort audit trail — record the rejection. Swallow IntegrityError
        # so the 401 response still goes back: a missing webhook_id collides with
        # any prior placeholder row under the (channel, webhook_id) UNIQUE.
        try:
            async with session.begin():
                session.add(
                    WebhookLog(
                        channel="shopify",
                        webhook_id=webhook_id or f"missing-{uuid4()}",
                        topic=topic or "unknown",
                        hmac_valid=False,
                        payload=None,
                        status=WebhookStatusEnum.REJECTED,
                    )
                )
        except IntegrityError:
            log.info(
                "shopify.webhook.rejection_audit_duplicate", webhook_id=webhook_id, topic=topic
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")

    try:
        payload: dict[str, Any] = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        log.warning("shopify.webhook.bad_json", webhook_id=webhook_id, error=str(exc))
        raise HTTPException(status_code=400, detail="malformed JSON") from exc

    normalized = ShopifyAdapter.normalize_webhook_order(payload)

    # Insert WebhookLog; on duplicate (redelivery) we ack without re-enqueueing.
    # The INSERT must commit BEFORE we enqueue, otherwise the handler — which
    # runs in its own session/transaction — cannot see the row to update its
    # status to PROCESSED.
    webhook_row = WebhookLog(
        channel="shopify",
        webhook_id=webhook_id,
        topic=topic,
        hmac_valid=True,
        payload=payload,
        status=WebhookStatusEnum.RECEIVED,
    )
    try:
        async with session.begin():
            session.add(webhook_row)
            await session.flush()
            webhook_log_id = webhook_row.id
    except IntegrityError:
        log.info("shopify.webhook.duplicate", webhook_id=webhook_id, topic=topic)
        return Response(status_code=status.HTTP_200_OK)

    await queue.enqueue(
        Task(
            name=PROCESS_SHOPIFY_WEBHOOK,
            payload=build_shopify_webhook_payload(
                webhook_log_id=webhook_log_id, normalized=normalized
            ),
        )
    )
    return Response(status_code=status.HTTP_200_OK)

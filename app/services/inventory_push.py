"""InventoryPushService — orchestrates per-SKU inventory pushes to channels.

Phase 1-B F1.4.

For every push:
1. Insert a SyncAttempt row (status='pending') so even a crash mid-call
   leaves a forensic trail.
2. Invoke `adapter.push_inventory(channel_sku, quantity)`. The adapter is
   responsible for its own retries/backoff/rate-limit; we treat its return
   as authoritative.
3. On return: flip status to 'succeeded', stamp finished_at, capture any
   response payload the adapter provides.
4. On exception: flip status to 'failed', capture error class/message,
   stamp finished_at, fire Slack notification at 'error' level.

The service does NOT commit; the caller controls transaction boundaries
so a push and its audit row succeed or fail atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChannelAdapter
from app.logging import get_logger
from app.models import (
    SyncAttempt,
    SyncAttemptStatusEnum,
    SyncAttemptTypeEnum,
)
from app.notifications.slack import SlackNotifier

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PushRequest:
    """Parameters for a single push_inventory call."""

    master_sku_id: int
    channel_sku: str
    quantity: int
    triggered_by: str
    parent_attempt_id: int | None = None


class InventoryPushService:
    def __init__(
        self,
        session: AsyncSession,
        notifier: SlackNotifier | None = None,
    ) -> None:
        self._session = session
        self._notifier = notifier

    async def push_single(
        self,
        adapter: ChannelAdapter,
        request: PushRequest,
    ) -> SyncAttempt:
        attempt = SyncAttempt(
            attempt_type=SyncAttemptTypeEnum.PUSH_INVENTORY.value,
            channel=adapter.channel,
            master_sku_id=request.master_sku_id,
            payload={
                "channel_sku": request.channel_sku,
                "quantity": request.quantity,
                "triggered_by": request.triggered_by,
            },
            status=SyncAttemptStatusEnum.PENDING.value,
            parent_attempt_id=request.parent_attempt_id,
            attempt_count=1,
        )
        self._session.add(attempt)
        await self._session.flush()

        try:
            response = await adapter.push_inventory(
                request.channel_sku,
                request.quantity,
            )
        except Exception as exc:  # noqa: BLE001 — we deliberately convert any
                                  # adapter failure into an auditable row
            await self._mark_failed(attempt, exc)
            return attempt

        await self._mark_succeeded(attempt, response)
        return attempt

    async def _mark_succeeded(
        self,
        attempt: SyncAttempt,
        response: Any,
    ) -> None:
        attempt.status = SyncAttemptStatusEnum.SUCCEEDED.value
        attempt.finished_at = datetime.now(UTC)
        if isinstance(response, dict):
            attempt.response_payload = response
        await self._session.flush()
        log.info(
            "inventory_push.succeeded",
            attempt_id=attempt.id,
            channel=attempt.channel,
            master_sku_id=attempt.master_sku_id,
        )

    async def _mark_failed(
        self,
        attempt: SyncAttempt,
        exc: BaseException,
    ) -> None:
        attempt.status = SyncAttemptStatusEnum.FAILED.value
        attempt.finished_at = datetime.now(UTC)
        attempt.error_code = exc.__class__.__name__
        # Truncate to a sane length; the full traceback already goes to logs.
        attempt.error_message = self._humanize_error(exc)
        await self._session.flush()
        log.warning(
            "inventory_push.failed",
            attempt_id=attempt.id,
            channel=attempt.channel,
            master_sku_id=attempt.master_sku_id,
            error_code=attempt.error_code,
            error_message=attempt.error_message,
        )
        if self._notifier is not None:
            await self._notifier.notify(
                level="error",
                title=f"在庫反映エラー ({attempt.channel})",
                message=(
                    f"SKU の在庫反映に失敗しました。同期エラー一覧から再実行してください。\n"
                    f"原因: {attempt.error_code}: {attempt.error_message}"
                ),
                fields=[
                    ("channel", attempt.channel or "?"),
                    ("master_sku_id", str(attempt.master_sku_id)),
                    ("channel_sku", attempt.payload.get("channel_sku", "?")),
                    ("attempt_id", str(attempt.id)),
                ],
            )

    @staticmethod
    def _humanize_error(exc: BaseException) -> str:
        """Convert a raw exception into a single short line for the audit row.

        The admin sync-errors page (F2.5) prefixes this with a Japanese
        guidance sentence so non-engineer staff can see what to do next.
        """
        msg = str(exc).strip()
        if not msg:
            return exc.__class__.__name__
        return msg[:500]

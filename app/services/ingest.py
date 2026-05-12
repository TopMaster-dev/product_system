"""OrderIngestService — channel-agnostic order ingestion pipeline.

End-to-end behavior:

1. Idempotently upserts the order on (channel, channel_order_id).
2. For each line item, resolves the master_sku_id via ChannelSkuMapping.
   - If mapped: persists the OrderItem with master_sku_id set and emits
     `order_consumed` (or no event for cancelled orders).
   - If unmapped: persists the OrderItem with master_sku_id NULL, registers
     a MappingAlert, and leaves the order in `pending_mapping` status.
3. Cancellation transitions emit `cancellation_returned` events for items
   that were previously consumed, using the same source identifiers — the
   UNIQUE constraint on inventory_events guarantees no double compensation.

The service does NOT commit; the caller controls transaction boundaries so
ingestion of a single channel order is all-or-nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import NormalizedOrder, NormalizedOrderLine
from app.models import (
    ChannelSkuMapping,
    MappingAlert,
    MappingAlertStatusEnum,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.inventory import EventSource, InventoryService


@dataclass(frozen=True, slots=True)
class IngestResult:
    order: Order
    created: bool
    pending_mapping_count: int
    consumed_count: int
    cancelled_count: int


_CANCEL_STATUSES = {"cancelled", "returned"}


class OrderIngestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)

    async def ingest(self, payload: NormalizedOrder) -> IngestResult:
        existing = await self._find_order(payload.channel, payload.channel_order_id)
        if existing is None:
            return await self._ingest_new(payload)
        return await self._ingest_update(existing, payload)

    # ---------- new orders ----------

    async def _ingest_new(self, payload: NormalizedOrder) -> IngestResult:
        is_cancelled = payload.status in _CANCEL_STATUSES
        order = Order(
            channel=payload.channel,
            channel_order_id=payload.channel_order_id,
            marketplace_id=payload.marketplace_id,
            status=payload.status,
            ordered_at=payload.ordered_at,
            raw_payload=payload.raw_payload,
        )
        self._session.add(order)
        await self._session.flush()

        pending = 0
        consumed = 0
        for line in payload.items:
            mapped_id = await self._lookup_mapping(payload.channel, line, payload.marketplace_id)
            item = OrderItem(
                order_id=order.id,
                line_id=line.line_id,
                channel_sku=line.channel_sku,
                master_sku_id=mapped_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                fulfillment_type=line.fulfillment_type,
            )
            self._session.add(item)
            await self._session.flush()

            if mapped_id is None:
                await self._record_alert(payload, line)
                pending += 1
                continue
            if is_cancelled:
                # Cancelled-on-arrival: never consumed, nothing to compensate.
                continue
            await self._inventory.consume_for_order_line(
                master_sku_id=mapped_id,
                quantity=line.quantity,
                source=EventSource(
                    channel=payload.channel,
                    order_id=payload.channel_order_id,
                    line_id=line.line_id,
                ),
                occurred_at=payload.ordered_at,
            )
            consumed += 1

        if pending and not is_cancelled:
            order.status = OrderStatusEnum.PENDING_MAPPING

        return IngestResult(
            order=order,
            created=True,
            pending_mapping_count=pending,
            consumed_count=consumed,
            cancelled_count=0,
        )

    # ---------- existing orders ----------

    async def _ingest_update(self, order: Order, payload: NormalizedOrder) -> IngestResult:
        cancelled = 0
        prev_cancelled = order.status in _CANCEL_STATUSES
        now_cancelled = payload.status in _CANCEL_STATUSES

        if payload.raw_payload is not None:
            order.raw_payload = payload.raw_payload

        if now_cancelled and not prev_cancelled:
            cancelled = await self._compensate_lines(order, payload.ordered_at)

        order.status = payload.status

        return IngestResult(
            order=order,
            created=False,
            pending_mapping_count=0,
            consumed_count=0,
            cancelled_count=cancelled,
        )

    async def _compensate_lines(self, order: Order, occurred_at: datetime) -> int:
        result = await self._session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.master_sku_id.is_not(None),
            ),
        )
        compensated = 0
        for item in result.scalars().all():
            assert item.master_sku_id is not None  # narrowed by WHERE
            applied = await self._inventory.cancel_order_line(
                master_sku_id=item.master_sku_id,
                quantity=item.quantity,
                source=EventSource(
                    channel=order.channel,
                    order_id=order.channel_order_id,
                    line_id=item.line_id,
                ),
                occurred_at=occurred_at,
            )
            if applied is not None:
                compensated += 1
        return compensated

    # ---------- helpers ----------

    async def _find_order(self, channel: str, channel_order_id: str) -> Order | None:
        result = await self._session.execute(
            select(Order).where(
                Order.channel == channel,
                Order.channel_order_id == channel_order_id,
            ),
        )
        return result.scalar_one_or_none()

    async def _lookup_mapping(
        self,
        channel: str,
        line: NormalizedOrderLine,
        marketplace_id: str | None,
    ) -> int | None:
        result = await self._session.execute(
            select(ChannelSkuMapping.master_sku_id).where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.channel_sku == line.channel_sku,
                ChannelSkuMapping.marketplace_id.is_(marketplace_id),
                ChannelSkuMapping.is_active.is_(True),
            ),
        )
        return result.scalar_one_or_none()

    async def _record_alert(
        self,
        payload: NormalizedOrder,
        line: NormalizedOrderLine,
    ) -> None:
        """Upsert the alert; UNIQUE on (channel, channel_sku, marketplace_id)."""
        alert = MappingAlert(
            channel=payload.channel,
            channel_sku=line.channel_sku,
            channel_product_id=line.channel_product_id,
            marketplace_id=payload.marketplace_id,
            status=MappingAlertStatusEnum.OPEN,
            first_seen_at=datetime.now(UTC),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(alert)
                await self._session.flush()
        except IntegrityError:
            # Already alerted — increment occurrence_count.
            existing = await self._session.execute(
                select(MappingAlert).where(
                    MappingAlert.channel == payload.channel,
                    MappingAlert.channel_sku == line.channel_sku,
                    MappingAlert.marketplace_id.is_(payload.marketplace_id),
                ),
            )
            row = existing.scalar_one()
            row.occurrence_count += 1
            if row.status == MappingAlertStatusEnum.RESOLVED:
                # A previously-resolved alert came back unmapped — reopen it.
                row.status = MappingAlertStatusEnum.OPEN

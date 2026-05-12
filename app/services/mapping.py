"""MappingService — resolve channel SKUs and replay pending orders.

When an order arrives whose `channel_sku` has no active mapping, ingestion
leaves the order in `pending_mapping` state and records a `MappingAlert`.
Once the operator resolves the alert by creating a `ChannelSkuMapping`, this
service backfills `master_sku_id` on the parked order items and emits
`order_consumed` events for them — preserving idempotency end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChannelSkuMapping,
    MappingAlert,
    MappingAlertStatusEnum,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.exceptions import MappingNotFoundError
from app.services.inventory import EventSource, InventoryService


class MappingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)

    async def resolve_alert(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
        marketplace_id: str | None = None,
        channel_product_id: str | None = None,
    ) -> int:
        """Map an unmapped channel SKU and replay all pending order lines.

        Returns the number of order lines that were replayed.
        """
        await self._upsert_mapping(
            channel=channel,
            channel_sku=channel_sku,
            master_sku_id=master_sku_id,
            marketplace_id=marketplace_id,
            channel_product_id=channel_product_id,
        )
        await self._close_alert(
            channel=channel,
            channel_sku=channel_sku,
            marketplace_id=marketplace_id,
            master_sku_id=master_sku_id,
        )
        return await self._replay_pending_lines(
            channel=channel,
            channel_sku=channel_sku,
            master_sku_id=master_sku_id,
        )

    async def find_master_sku_id(
        self,
        *,
        channel: str,
        channel_sku: str,
        marketplace_id: str | None = None,
    ) -> int | None:
        result = await self._session.execute(
            select(ChannelSkuMapping.master_sku_id).where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.channel_sku == channel_sku,
                ChannelSkuMapping.marketplace_id.is_(marketplace_id),
                ChannelSkuMapping.is_active.is_(True),
            ),
        )
        return result.scalar_one_or_none()

    # ---------- internals ----------

    async def _upsert_mapping(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
        marketplace_id: str | None,
        channel_product_id: str | None,
    ) -> ChannelSkuMapping:
        result = await self._session.execute(
            select(ChannelSkuMapping).where(
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.channel_sku == channel_sku,
                ChannelSkuMapping.marketplace_id.is_(marketplace_id),
            ),
        )
        mapping = result.scalar_one_or_none()
        if mapping is not None:
            mapping.master_sku_id = master_sku_id
            mapping.is_active = True
            if channel_product_id is not None:
                mapping.channel_product_id = channel_product_id
            return mapping

        mapping = ChannelSkuMapping(
            channel=channel,
            channel_sku=channel_sku,
            channel_product_id=channel_product_id,
            marketplace_id=marketplace_id,
            master_sku_id=master_sku_id,
            is_active=True,
        )
        self._session.add(mapping)
        await self._session.flush()
        return mapping

    async def _close_alert(
        self,
        *,
        channel: str,
        channel_sku: str,
        marketplace_id: str | None,
        master_sku_id: int,
    ) -> None:
        await self._session.execute(
            update(MappingAlert)
            .where(
                MappingAlert.channel == channel,
                MappingAlert.channel_sku == channel_sku,
                MappingAlert.marketplace_id.is_(marketplace_id),
                MappingAlert.status == MappingAlertStatusEnum.OPEN,
            )
            .values(
                status=MappingAlertStatusEnum.RESOLVED,
                resolved_master_sku_id=master_sku_id,
                resolved_at=datetime.now(UTC),
            ),
        )

    async def _replay_pending_lines(
        self,
        *,
        channel: str,
        channel_sku: str,
        master_sku_id: int,
    ) -> int:
        """Backfill master_sku_id on parked items and emit consumption events."""
        rows = await self._session.execute(
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.channel == channel,
                Order.status == OrderStatusEnum.PENDING_MAPPING,
                OrderItem.channel_sku == channel_sku,
                OrderItem.master_sku_id.is_(None),
            ),
        )
        replayed = 0
        for item, order in rows.all():
            item.master_sku_id = master_sku_id
            await self._inventory.consume_for_order_line(
                master_sku_id=master_sku_id,
                quantity=item.quantity,
                source=EventSource(
                    channel=order.channel,
                    order_id=order.channel_order_id,
                    line_id=item.line_id,
                ),
                occurred_at=order.ordered_at,
            )
            order.status = OrderStatusEnum.CONFIRMED
            replayed += 1
        return replayed


__all__ = ["MappingNotFoundError", "MappingService"]

"""One-off: reprocess orders stuck in `pending_mapping` whose channel SKUs
now have channel_sku_mappings.

For each pending_mapping order:
  - For each item with master_sku_id IS NULL:
      look up mapping; if found, set master_sku_id and emit a consume event
      (unless the order is cancelled/returned, in which case skip the event).
  - If all items end up mapped AND the order isn't cancelled/returned,
    transition status from pending_mapping → confirmed.

Idempotent — the InventoryService UNIQUE on (event_type, source_channel,
source_order_id, source_line_id) blocks duplicate consume events, so re-running
is safe.

Usage:
    py -m app.cli.reprocess_pending_orders [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    ChannelSkuMapping,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.inventory import EventSource, InventoryService

log = get_logger(__name__)
_CANCEL_STATUSES = {"cancelled", "returned"}


async def run(*, dry_run: bool = False, limit: int | None = None) -> int:
    log.info("reprocess.start", dry_run=dry_run, limit=limit)
    now = datetime.now(UTC)

    orders_touched = 0
    items_mapped = 0
    consume_events = 0
    orders_promoted = 0
    orders_still_partial = 0

    async with async_session_factory() as session, session.begin():
        inventory = InventoryService(session)

        stmt = (
            select(Order)
            .where(Order.status == OrderStatusEnum.PENDING_MAPPING)
            .order_by(Order.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        pending_orders = list(result.scalars().all())
        log.info("reprocess.fetched", count=len(pending_orders))

        for order in pending_orders:
            items_result = await session.execute(
                select(OrderItem).where(OrderItem.order_id == order.id),
            )
            items = list(items_result.scalars().all())
            unresolved_before = sum(1 for i in items if i.master_sku_id is None)
            if unresolved_before == 0:
                # Status got out of sync with items; just promote it.
                if order.status == OrderStatusEnum.PENDING_MAPPING:
                    order.status = OrderStatusEnum.CONFIRMED
                    orders_promoted += 1
                continue

            is_cancelled = order.status in _CANCEL_STATUSES
            still_unresolved = 0

            for item in items:
                if item.master_sku_id is not None:
                    continue
                lookup = await session.execute(
                    select(ChannelSkuMapping.master_sku_id).where(
                        ChannelSkuMapping.channel == order.channel,
                        ChannelSkuMapping.channel_sku == item.channel_sku,
                        ChannelSkuMapping.marketplace_id.is_(order.marketplace_id),
                        ChannelSkuMapping.is_active.is_(True),
                    ),
                )
                mid = lookup.scalar_one_or_none()
                if mid is None:
                    still_unresolved += 1
                    continue

                if not dry_run:
                    item.master_sku_id = mid
                items_mapped += 1

                if is_cancelled:
                    continue

                if not dry_run:
                    ev = await inventory.consume_for_order_line(
                        master_sku_id=mid,
                        quantity=item.quantity,
                        source=EventSource(
                            channel=order.channel,
                            order_id=order.channel_order_id,
                            line_id=item.line_id,
                        ),
                        occurred_at=order.ordered_at or now,
                    )
                    if ev is not None:
                        consume_events += 1

            orders_touched += 1

            if still_unresolved == 0 and not dry_run:
                if order.status == OrderStatusEnum.PENDING_MAPPING and not is_cancelled:
                    order.status = OrderStatusEnum.CONFIRMED
                    orders_promoted += 1
            elif still_unresolved > 0:
                orders_still_partial += 1

        if dry_run:
            await session.rollback()

    log.info("reprocess.done",
             orders_touched=orders_touched,
             items_mapped=items_mapped,
             consume_events=consume_events,
             orders_promoted=orders_promoted,
             orders_still_partial=orders_still_partial)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reprocess pending_mapping orders against current mappings",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, limit=args.limit)))


if __name__ == "__main__":
    main()

"""Bulk re-resolution against a real database (W7-5).

The unit tests fix the decisions; these fix what the decisions do to rows —
in particular that the historical pass leaves `inventory_events` completely
alone while still repairing the sales attribution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services import InventoryService
from app.services.mapping import reresolve_unmapped_lines

pytestmark = pytest.mark.integration


async def _seed(
    session,
    *,
    channel_order_id: str,
    skus: list[str],
    status: str = OrderStatusEnum.PENDING_MAPPING,
    quantity: int = 2,
) -> Order:
    order = Order(
        channel="rakuten",
        channel_order_id=channel_order_id,
        status=status,
        ordered_at=datetime(2026, 3, 4, tzinfo=UTC),
    )
    session.add(order)
    await session.flush()
    for index, sku in enumerate(skus, start=1):
        session.add(
            OrderItem(
                order_id=order.id,
                line_id=f"L-{index}",
                channel_sku=sku,
                quantity=quantity,
                unit_price=Decimal("1200.00"),
            )
        )
    await session.flush()
    return order


async def _map(session, *, channel_sku: str, sku_code: str) -> MasterSku:
    master = MasterSku(sku_code=sku_code, name=sku_code)
    session.add(master)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            channel="rakuten",
            channel_sku=channel_sku,
            master_sku_id=master.id,
            is_active=True,
        )
    )
    await session.flush()
    return master


async def _event_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(InventoryEvent)) or 0


async def test_the_historical_pass_repairs_attribution_without_touching_stock(
    db_session,
) -> None:
    """The 管理番号 correction case: the old value is mapped onto the right
    master, and months of sales become attributable again — while the shelf,
    already counted, is left exactly as it is."""
    master = await _map(db_session, channel_sku="r-sku00000041", sku_code="N108-45")
    order = await _seed(db_session, channel_order_id="R-OLD", skus=["r-sku00000041"])
    before = await _event_count(db_session)

    outcome = await reresolve_unmapped_lines(db_session, apply_stock=False)

    assert outcome.lines_filled == 1
    assert outcome.stock_events == 0
    assert await _event_count(db_session) == before
    assert await InventoryService(db_session).get_current_stock(master.id) == 0

    item = (
        await db_session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    ).scalar_one()
    assert item.master_sku_id == master.id


async def test_the_live_pass_moves_stock(db_session) -> None:
    master = await _map(db_session, channel_sku="LIVE-1", sku_code="LIVE-MASTER")
    await _seed(db_session, channel_order_id="R-LIVE", skus=["LIVE-1"])

    outcome = await reresolve_unmapped_lines(db_session, apply_stock=True)

    assert outcome.stock_events == 1
    assert await InventoryService(db_session).get_current_stock(master.id) == -2


async def test_running_the_live_pass_twice_does_not_double_count(db_session) -> None:
    master = await _map(db_session, channel_sku="TWICE-1", sku_code="TWICE-MASTER")
    await _seed(db_session, channel_order_id="R-TWICE", skus=["TWICE-1"])

    await reresolve_unmapped_lines(db_session, apply_stock=True)
    stock = await InventoryService(db_session).get_current_stock(master.id)

    # Nothing is unmapped any more, so the second pass finds no work at all.
    again = await reresolve_unmapped_lines(db_session, apply_stock=True)
    assert again.lines_filled == 0
    assert await InventoryService(db_session).get_current_stock(master.id) == stock


async def test_a_line_stranded_on_a_confirmed_order_is_still_picked_up(db_session) -> None:
    """Why the scan is by line. These exist because resolving one alert used to
    confirm the whole order, and no status-based query can reach them."""
    master = await _map(db_session, channel_sku="STRANDED", sku_code="STRANDED-MASTER")
    order = await _seed(
        db_session,
        channel_order_id="R-STRANDED",
        skus=["STRANDED"],
        status=OrderStatusEnum.CONFIRMED,
    )

    outcome = await reresolve_unmapped_lines(db_session, apply_stock=False)

    assert outcome.lines_filled == 1
    item = (
        await db_session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    ).scalar_one()
    assert item.master_sku_id == master.id


async def test_a_partly_mapped_order_stays_pending(db_session) -> None:
    await _map(db_session, channel_sku="KNOWN", sku_code="KNOWN-MASTER")
    order = await _seed(db_session, channel_order_id="R-HALF", skus=["KNOWN", "UNKNOWN"])

    outcome = await reresolve_unmapped_lines(db_session, apply_stock=False)

    assert outcome.lines_filled == 1
    assert outcome.orders_settled == 0
    await db_session.refresh(order)
    assert order.status == OrderStatusEnum.PENDING_MAPPING
    assert outcome.unresolved[("rakuten", "UNKNOWN")].lines == 1


async def test_the_channel_filter_leaves_other_channels_untouched(db_session) -> None:
    await _map(db_session, channel_sku="RAK-ONLY", sku_code="RAK-MASTER")
    await _seed(db_session, channel_order_id="R-FILTER", skus=["RAK-ONLY"])

    outcome = await reresolve_unmapped_lines(db_session, apply_stock=False, channel="shopify")

    assert outcome.lines_filled == 0

"""The review material's queries against a real database.

The per-channel table is what the client compares with their own 受注一覧, so
it must be the same arithmetic as the reconciliation the screens are checked
against — split, not re-derived. And the walkthrough's "shared by N parents"
is the point of the walkthrough; it must count the sharing, not the row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.cli.inspect_review_materials import bundle_examples, channel_days, trace_latest_line
from app.models import (
    BundleComponent,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.analytics_audit import recompute_from_orders
from app.services.timeframe import Period

pytestmark = pytest.mark.integration

DAY = date(2026, 9, 15)
#: 08:30 JST on DAY — before 09:00, so a naive UTC date would misfile it.
EARLY = datetime(2026, 9, 14, 23, 30, tzinfo=UTC)
NOON = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
ONE_DAY = Period(DAY, DAY, "day")


async def _sku(session, code: str, **kw) -> MasterSku:
    sku = MasterSku(sku_code=code, name=code, **kw)
    session.add(sku)
    await session.flush()
    return sku


async def _order(
    session,
    order_id: str,
    *,
    channel: str,
    ordered_at: datetime = NOON,
    status: str = OrderStatusEnum.CONFIRMED,
    lines: list[tuple[int | None, int, str]],
) -> Order:
    order = Order(channel=channel, channel_order_id=order_id, status=status, ordered_at=ordered_at)
    session.add(order)
    await session.flush()
    for index, (sku_id, qty, price) in enumerate(lines):
        session.add(
            OrderItem(
                order_id=order.id,
                line_id=f"L{index}",
                channel_sku=f"CH-{order_id}-{index}",
                master_sku_id=sku_id,
                quantity=qty,
                unit_price=Decimal(price),
            )
        )
    await session.flush()
    return order


async def test_the_channels_add_up_to_the_reconciliation(db_session) -> None:
    a = await _sku(db_session, "A")
    await _order(db_session, "R-1", channel="rakuten", lines=[(a.id, 2, "1100"), (None, 1, "500")])
    await _order(db_session, "R-2", channel="rakuten", ordered_at=EARLY, lines=[(a.id, 1, "1100")])
    await _order(
        db_session,
        "R-3",
        channel="rakuten",
        status=OrderStatusEnum.CANCELLED,
        lines=[(a.id, 5, "1100")],
    )
    await _order(db_session, "S-1", channel="shopify", lines=[(a.id, 3, "990")])

    rows = await channel_days(db_session, ONE_DAY)
    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert {(r.day, r.channel) for r in rows} == {(DAY, "rakuten"), (DAY, "shopify")}
    assert sum(r.sales for r in rows) == raw.gross_sales_jpy
    assert sum(r.quantity for r in rows) == raw.sold_quantity
    assert sum(r.orders for r in rows) == raw.order_count
    assert sum(r.unmapped for r in rows) == raw.unmapped_sales_jpy

    rakuten = next(r for r in rows if r.channel == "rakuten")
    assert (rakuten.orders, rakuten.cancelled_orders) == (2, 1)
    assert rakuten.sales == Decimal("3300")
    assert rakuten.unmapped == Decimal("500")
    # The totals are what the client holds against RMS, which knows no mapping.
    assert (rakuten.unmapped_quantity, rakuten.total_quantity) == (1, 4)
    assert rakuten.total_sales == Decimal("3800")


async def test_the_walkthrough_counts_every_parent_sharing_a_component(db_session) -> None:
    pool = await _sku(db_session, "POOL")
    db_session.add(InventorySnapshot(master_sku_id=pool.id, on_hand_qty=12))
    short = await _sku(db_session, "ANKLET-23", is_bundle=True)
    long = await _sku(db_session, "ANKLET-25", is_bundle=True)
    for parent in (short, long):
        db_session.add(
            BundleComponent(
                bundle_master_sku_id=parent.id, component_master_sku_id=pool.id, quantity_per=1
            )
        )
    await db_session.flush()

    order = await _order(db_session, "R-9", channel="rakuten", lines=[(short.id, 2, "3300")])
    db_session.add(
        InventoryEvent(
            master_sku_id=pool.id,
            event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
            quantity_delta=-2,
            source_channel="rakuten",
            source_order_id=order.channel_order_id,
            source_line_id="L0",
            occurred_at=NOON,
        )
    )
    await db_session.flush()

    since = NOON - timedelta(days=1)
    examples = await bundle_examples(db_session, since=since)

    assert [e.sku_code for e in examples] == ["ANKLET-23"]
    example = examples[0]
    assert example.quantity == 2
    assert example.events_on_parent == 0
    assert [(c.sku_code, c.on_hand, c.shared_by) for c in example.components] == [("POOL", 12, 2)]

    traced = await trace_latest_line(db_session, short.id, since=since)
    assert traced is not None
    line, events = traced
    assert line.channel_order_id == "R-9"
    assert [(code, delta) for code, _, delta in events] == [("POOL", -2)]

"""Integration tests — AnalyticsRollupService against real Postgres.

These are the W4 exit criteria, written as behaviour:

* a 02:30 JST order rolls up on the JST day, not the UTC one
* rebuilding a range twice produces identical rows (idempotent)
* cancelling a 60-day-old order puts THAT day back in the rebuild set
* a set sale counts components once in stock and the parent once in sales
* an archived SKU keeps its past sales but leaves the stock grid
* unmapped lines land in daily_unmapped_sales rather than vanishing
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    BundleComponent,
    DailyKpiSnapshot,
    DailyUnmappedSales,
    InventoryEvent,
    InventoryEventTypeEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
    SkuDailySales,
    SkuDailyStock,
)
from app.services.analytics_rollup import AnalyticsRollupService
from app.services.timeframe import jst_day_bounds

pytestmark = pytest.mark.integration


async def _sku(factory, code: str, **kwargs) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=code, **kwargs)
        session.add(sku)
        await session.flush()
        return sku.id


async def _event(factory, sku_id: int, delta: int, occurred: datetime, *, kind=None) -> None:
    async with factory() as session, session.begin():
        session.add(
            InventoryEvent(
                master_sku_id=sku_id,
                event_type=kind or InventoryEventTypeEnum.ORDER_CONSUMED,
                quantity_delta=delta,
                source_channel="shopify",
                source_order_id=f"EV-{sku_id}-{occurred.isoformat()}-{delta}",
                source_line_id="L1",
                occurred_at=occurred,
            )
        )


async def _order(
    factory,
    order_id: str,
    ordered_at: datetime,
    lines: list[tuple[int | None, str, int, str]],
    *,
    status=OrderStatusEnum.CONFIRMED,
) -> int:
    async with factory() as session, session.begin():
        order = Order(
            channel="shopify",
            channel_order_id=order_id,
            status=status,
            ordered_at=ordered_at,
        )
        session.add(order)
        await session.flush()
        for i, (sku_id, channel_sku, qty, price) in enumerate(lines):
            session.add(
                OrderItem(
                    order_id=order.id,
                    line_id=f"L{i}",
                    channel_sku=channel_sku,
                    master_sku_id=sku_id,
                    quantity=qty,
                    unit_price=Decimal(price),
                )
            )
        return order.id


async def _rebuild(factory, days: list[date]) -> None:
    """One transaction PER DAY, as production does it."""
    for day in days:
        async with factory() as session, session.begin():
            await AnalyticsRollupService(session).rebuild_day(day)


# --- JST day boundary ----------------------------------------------------


async def test_an_order_at_0230_jst_rolls_up_on_the_jst_day(_test_engine) -> None:
    """02:30 JST on 08-02 is 17:30 UTC on 08-01. A UTC-based rollup files it
    under the 1st; roughly a third of daily volume falls in that window."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "JST-1")
    await _order(
        factory, "JST-ORD-1", datetime(2026, 8, 1, 17, 30, tzinfo=UTC), [(sku, "c", 2, "1000")]
    )

    await _rebuild(factory, [date(2026, 8, 1), date(2026, 8, 2)])

    async with factory() as session:
        rows = (
            (await session.execute(select(SkuDailySales).where(SkuDailySales.master_sku_id == sku)))
            .scalars()
            .all()
        )
    assert [r.stat_date for r in rows] == [date(2026, 8, 2)]
    assert rows[0].quantity == 2


# --- idempotency ---------------------------------------------------------


async def test_rebuilding_the_same_day_twice_is_identical(_test_engine) -> None:
    """DELETE+INSERT under a UNIQUE key. A second run must neither double the
    figures nor raise."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "IDEM-1")
    day = date(2026, 8, 5)
    start, _ = jst_day_bounds(day)
    await _order(factory, "IDEM-ORD", start + timedelta(hours=3), [(sku, "c", 3, "500")])
    await _event(factory, sku, -3, start + timedelta(hours=3))

    async def snapshot() -> tuple:
        async with factory() as session:
            sales = (
                await session.execute(
                    select(SkuDailySales.quantity, SkuDailySales.gross_sales_jpy).where(
                        SkuDailySales.stat_date == day
                    )
                )
            ).all()
            stock = await session.scalar(
                select(func.count())
                .select_from(SkuDailyStock)
                .where(SkuDailyStock.stat_date == day)
            )
            kpi = await session.scalar(
                select(func.count())
                .select_from(DailyKpiSnapshot)
                .where(DailyKpiSnapshot.stat_date == day)
            )
        return sales, stock, kpi

    await _rebuild(factory, [day])
    first = await snapshot()
    await _rebuild(factory, [day])
    second = await snapshot()

    assert first == second
    assert first[2] == 1, "exactly one KPI row per day"


# --- the backdated case --------------------------------------------------


async def test_cancelling_an_old_order_puts_that_day_back_in_the_rebuild_set(
    _test_engine,
) -> None:
    """The property a fixed 3-day window cannot have.

    An order placed 60 days ago, cancelled today, must correct the day the sale
    was BOOKED. The window is derived from orders.updated_at, so the old
    ordered_at day comes back into scope.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "BACKDATE-1")
    old_day = date(2026, 6, 1)
    start, _ = jst_day_bounds(old_day)
    order_id = await _order(
        factory, "BACKDATE-ORD", start + timedelta(hours=5), [(sku, "c", 4, "2500")]
    )

    await _rebuild(factory, [old_day])
    async with factory() as session:
        before = await session.scalar(
            select(SkuDailySales.quantity).where(
                SkuDailySales.stat_date == old_day, SkuDailySales.master_sku_id == sku
            )
        )
    assert before == 4

    watermark = datetime.now(UTC)

    # Cancel it today. Only updated_at moves; ordered_at stays in June.
    async with factory() as session, session.begin():
        order = await session.get(Order, order_id)
        order.status = OrderStatusEnum.CANCELLED

    async with factory() as session:
        due = await AnalyticsRollupService(session).dates_to_rebuild(watermark)
    assert old_day in due, "the June day must return to the rebuild set"

    await _rebuild(factory, [old_day])
    async with factory() as session:
        row = (
            await session.execute(
                select(SkuDailySales).where(
                    SkuDailySales.stat_date == old_day, SkuDailySales.master_sku_id == sku
                )
            )
        ).scalar_one()
    assert row.quantity == 0, "the cancelled sale no longer counts"
    assert row.cancelled_quantity == 4, "but it stays visible as cancelled"


async def test_a_backdated_event_rebuilds_the_day_it_claims(_test_engine) -> None:
    """Production holds events whose occurred_at is months before created_at —
    that is what the 遡及 badge marks. The window keys on created_at to FIND
    them and occurred_at to decide which day to fix."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "BACKDATE-EV")
    watermark = datetime.now(UTC)

    old_day = date(2026, 6, 15)
    start, _ = jst_day_bounds(old_day)
    await _event(factory, sku, -2, start + timedelta(hours=2))  # created now, occurred in June

    async with factory() as session:
        due = await AnalyticsRollupService(session).dates_to_rebuild(watermark)
    assert old_day in due


# --- bundles -------------------------------------------------------------


async def test_a_set_sale_counts_components_once_and_the_parent_zero(_test_engine) -> None:
    """Stock and sales split the difference: the parent is where the sale
    happened, the components are what moved. Counting both would inflate one
    of them."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    parent = await _sku(factory, "SET-PARENT", is_bundle=True)
    comp_a = await _sku(factory, "SET-COMP-A")
    comp_b = await _sku(factory, "SET-COMP-B")
    async with factory() as session, session.begin():
        session.add_all(
            [
                BundleComponent(
                    bundle_master_sku_id=parent, component_master_sku_id=comp_a, quantity_per=1
                ),
                BundleComponent(
                    bundle_master_sku_id=parent, component_master_sku_id=comp_b, quantity_per=1
                ),
            ]
        )

    day = date(2026, 8, 7)
    start, _ = jst_day_bounds(day)
    await _order(factory, "SET-ORD", start + timedelta(hours=4), [(parent, "set", 1, "9000")])
    # The fan-out moves the COMPONENTS, never the parent.
    await _event(factory, comp_a, -1, start + timedelta(hours=4))
    await _event(factory, comp_b, -1, start + timedelta(hours=4))

    await _rebuild(factory, [day])

    async with factory() as session:
        stock_skus = {
            s
            for (s,) in (
                await session.execute(
                    select(SkuDailyStock.master_sku_id).where(SkuDailyStock.stat_date == day)
                )
            ).all()
        }
        sales = (
            (await session.execute(select(SkuDailySales).where(SkuDailySales.stat_date == day)))
            .scalars()
            .all()
        )

    assert parent not in stock_skus, "a bundle parent holds no stock of its own"
    assert {comp_a, comp_b} <= stock_skus
    assert [r.master_sku_id for r in sales] == [parent], "revenue belongs to the set"
    assert sales[0].gross_sales_jpy == Decimal("9000.00")


# --- archived: history kept, present dropped -----------------------------


async def test_an_archived_sku_keeps_past_sales_but_leaves_the_stock_grid(
    _test_engine,
) -> None:
    """The two-rule split. Dropping its sales would erase revenue from every
    comparison spanning the 2026-07-20 cutover; keeping it in stock would count
    a retired product in today's fleet."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "ARCH-1")
    day = date(2026, 8, 8)
    start, _ = jst_day_bounds(day)
    await _order(factory, "ARCH-ORD", start + timedelta(hours=2), [(sku, "c", 5, "1200")])

    await _rebuild(factory, [day])
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkuDailyStock)
                .where(SkuDailyStock.stat_date == day, SkuDailyStock.master_sku_id == sku)
            )
            == 1
        )

    # Retire it, then rebuild the same historical day.
    async with factory() as session, session.begin():
        master = await session.get(MasterSku, sku)
        master.archived_at = datetime.now(UTC)
    await _rebuild(factory, [day])

    async with factory() as session:
        in_stock = await session.scalar(
            select(func.count())
            .select_from(SkuDailyStock)
            .where(SkuDailyStock.stat_date == day, SkuDailyStock.master_sku_id == sku)
        )
        sold = await session.scalar(
            select(SkuDailySales.quantity).where(
                SkuDailySales.stat_date == day, SkuDailySales.master_sku_id == sku
            )
        )
    assert in_stock == 0, "archived SKUs leave the stock grid"
    assert sold == 5, "but their past sales remain"


# --- unmapped ------------------------------------------------------------


async def test_unmapped_lines_are_captured_not_dropped(_test_engine) -> None:
    """156 such alerts exist in production. Without this table their revenue
    would simply be missing from every total, by an amount nobody can see."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    day = date(2026, 8, 9)
    start, _ = jst_day_bounds(day)
    await _order(
        factory, "UNMAPPED-ORD", start + timedelta(hours=6), [(None, "ghost-sku", 3, "800")]
    )

    await _rebuild(factory, [day])

    async with factory() as session:
        row = (
            await session.execute(
                select(DailyUnmappedSales).where(DailyUnmappedSales.stat_date == day)
            )
        ).scalar_one()
        kpi = (
            await session.execute(select(DailyKpiSnapshot).where(DailyKpiSnapshot.stat_date == day))
        ).scalar_one()

    assert row.channel_sku == "ghost-sku"
    assert row.quantity == 3
    assert row.gross_sales_jpy == Decimal("2400.00")
    assert kpi.unmapped_sales_jpy == Decimal("2400.00"), "surfaced beside the headline figure"


async def test_kpi_counts_each_order_once_regardless_of_line_count(_test_engine) -> None:
    """order_count comes from orders directly. Summing sku_daily_sales.order_count
    would count a three-line order three times."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    a = await _sku(factory, "MULTI-A")
    b = await _sku(factory, "MULTI-B")
    day = date(2026, 8, 10)
    start, _ = jst_day_bounds(day)
    await _order(
        factory,
        "MULTI-ORD",
        start + timedelta(hours=1),
        [(a, "ca", 1, "1000"), (b, "cb", 2, "500")],
    )

    await _rebuild(factory, [day])
    async with factory() as session:
        kpi = (
            await session.execute(select(DailyKpiSnapshot).where(DailyKpiSnapshot.stat_date == day))
        ).scalar_one()
    assert kpi.order_count == 1
    assert kpi.sold_quantity == 3
    assert kpi.gross_sales_jpy == Decimal("2000.00")


async def test_stock_carries_forward_across_days(_test_engine) -> None:
    """A day with no movement still gets a row, holding yesterday's balance."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "CARRY-1")
    d1, d2 = date(2026, 8, 11), date(2026, 8, 12)
    start, _ = jst_day_bounds(d1)
    await _event(
        factory, sku, +10, start + timedelta(hours=1), kind=InventoryEventTypeEnum.STOCKTAKE
    )

    await _rebuild(factory, [d1, d2])
    async with factory() as session:
        rows = {
            r.stat_date: r.on_hand_qty
            for r in (
                await session.execute(
                    select(SkuDailyStock).where(SkuDailyStock.master_sku_id == sku)
                )
            )
            .scalars()
            .all()
        }
    assert rows[d1] == 10
    assert rows[d2] == 10, "no movement on day 2, balance carried"

"""Integration tests — the shape of the analytics rollup tables.

Constraints here are not decoration: the rollup rebuilds a day by DELETE+INSERT,
so uniqueness on (day, key) is what stops a re-run doubling a day's figures. One
absence is equally deliberate — `analytics_rollup_runs` must NOT be unique on
(job, date), because the hourly job rebuilds recent days on purpose.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    AnalyticsRollupRun,
    DailyKpiSnapshot,
    DailyUnmappedSales,
    MasterSku,
    SkuDailySales,
    SkuDailyStock,
)

pytestmark = pytest.mark.integration

DAY = date(2026, 8, 2)


async def _seed_sku(factory, code: str) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=code)
        session.add(sku)
        await session.flush()
        return sku.id


async def test_stock_is_unique_per_day_and_sku(_test_engine) -> None:
    """The rollup rebuilds a day with DELETE+INSERT. Without this, a crash
    between the two — or a concurrent second run — silently doubles the day."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _seed_sku(factory, "ROLL-STOCK-1")

    async with factory() as session, session.begin():
        session.add(SkuDailyStock(stat_date=DAY, master_sku_id=sku, on_hand_qty=5))

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(SkuDailyStock(stat_date=DAY, master_sku_id=sku, on_hand_qty=9))


async def test_sales_are_unique_per_day_channel_and_sku(_test_engine) -> None:
    """Channel is part of the key: the same SKU legitimately sells on Shopify
    and Rakuten on the same day, and those must be separate rows."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _seed_sku(factory, "ROLL-SALES-1")

    async with factory() as session, session.begin():
        session.add_all(
            [
                SkuDailySales(
                    stat_date=DAY,
                    channel="shopify",
                    master_sku_id=sku,
                    quantity=2,
                    gross_sales_jpy=Decimal("3000"),
                ),
                SkuDailySales(
                    stat_date=DAY,
                    channel="rakuten",
                    master_sku_id=sku,
                    quantity=1,
                    gross_sales_jpy=Decimal("1500"),
                ),
            ]
        )

    async with factory() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(SkuDailySales)
            .where(SkuDailySales.master_sku_id == sku)
        )
    assert rows == 2, "the same SKU must be able to sell on two channels the same day"

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                SkuDailySales(stat_date=DAY, channel="shopify", master_sku_id=sku, quantity=99)
            )


async def test_unmapped_sales_survive_without_a_master(_test_engine) -> None:
    """The whole point of the table: a line with no master_sku_id has nowhere
    else to go, and dropping it makes the analytics disagree with real revenue
    by an amount nobody can see."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        session.add(
            DailyUnmappedSales(
                stat_date=DAY,
                channel="rakuten",
                channel_sku="r-sku00000042",
                order_count=2,
                quantity=3,
                gross_sales_jpy=Decimal("4500"),
            )
        )

    async with factory() as session:
        row = (
            await session.execute(
                select(DailyUnmappedSales).where(DailyUnmappedSales.channel_sku == "r-sku00000042")
            )
        ).scalar_one()
    assert row.gross_sales_jpy == Decimal("4500.00")


async def test_kpi_is_one_row_per_day(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        session.add(DailyKpiSnapshot(stat_date=DAY, total_on_hand_qty=100, sku_count=650))

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(DailyKpiSnapshot(stat_date=DAY, total_on_hand_qty=200, sku_count=650))


async def test_rollup_runs_allow_repeats_for_the_same_day(_test_engine) -> None:
    """The ABSENCE of a constraint, asserted deliberately.

    The hourly job rebuilds recent days every hour by design. A unique on
    (job_name, stat_date) would make normal operation raise IntegrityError —
    so uniqueness lives on the data tables, not the execution log.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    now = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)

    async with factory() as session, session.begin():
        for _ in range(3):
            session.add(
                AnalyticsRollupRun(
                    job_name="hourly",
                    first_date=DAY,
                    last_date=DAY,
                    days_rebuilt=1,
                    status="success",
                    started_at=now,
                )
            )

    async with factory() as session:
        runs = await session.scalar(
            select(func.count())
            .select_from(AnalyticsRollupRun)
            .where(AnalyticsRollupRun.job_name == "hourly")
        )
    assert runs == 3


async def test_cost_columns_are_reserved_and_nullable(_test_engine) -> None:
    """原価 has not been supplied (P2-014). The columns exist so enabling stock
    valuation later is a rebuild, not an ALTER on a table holding a year of
    daily rows."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _seed_sku(factory, "ROLL-COST-1")

    async with factory() as session, session.begin():
        session.add(SkuDailyStock(stat_date=DAY, master_sku_id=sku, on_hand_qty=7))

    async with factory() as session:
        row = (
            await session.execute(select(SkuDailyStock).where(SkuDailyStock.master_sku_id == sku))
        ).scalar_one()
    assert row.unit_cost_jpy is None
    assert row.stock_value_jpy is None


async def test_sales_can_go_negative_for_a_refund_day(_test_engine) -> None:
    """Cancellations are subtracted rather than deleted, so a day can end
    negative. That is a refund, not a bug — clamping it at zero would break
    reconciliation against the shop's own figures."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _seed_sku(factory, "ROLL-REFUND-1")

    async with factory() as session, session.begin():
        session.add(
            SkuDailySales(
                stat_date=DAY,
                channel="shopify",
                master_sku_id=sku,
                quantity=-2,
                gross_sales_jpy=Decimal("-3000"),
                cancelled_quantity=2,
                cancelled_sales_jpy=Decimal("3000"),
            )
        )

    async with factory() as session:
        row = (
            await session.execute(select(SkuDailySales).where(SkuDailySales.master_sku_id == sku))
        ).scalar_one()
    assert row.quantity == -2
    assert row.gross_sales_jpy == Decimal("-3000.00")

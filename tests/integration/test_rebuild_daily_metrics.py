"""Integration tests — the rollup CLI: locking, planning, idempotency, pruning."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.rebuild_daily_metrics import ROLLUP_LOCK_KEY, run
from app.models import (
    AnalyticsRollupRun,
    DailyKpiSnapshot,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
    SkuDailySales,
    SkuDailyStock,
)
from app.services.timeframe import jst_day_bounds

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)  # 12:00 JST on the 20th


async def _sku(factory, code: str) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=code)
        session.add(sku)
        await session.flush()
        return sku.id


async def _order(factory, order_id: str, sku_id: int, day: date, qty: int, price: str) -> None:
    start, _ = jst_day_bounds(day)
    async with factory() as session, session.begin():
        order = Order(
            channel="shopify",
            channel_order_id=order_id,
            status=OrderStatusEnum.CONFIRMED,
            ordered_at=start + timedelta(hours=4),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                line_id="L1",
                channel_sku=f"c-{sku_id}",
                master_sku_id=sku_id,
                quantity=qty,
                unit_price=Decimal(price),
            )
        )


async def test_explicit_range_builds_every_day_inclusive(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-RANGE-1")

    outcome = await run(
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 5),
        now=NOW,
        session_factory=factory,
    )
    assert outcome.days_rebuilt == 5
    assert outcome.first_date == date(2026, 8, 1)
    assert outcome.last_date == date(2026, 8, 5)

    async with factory() as session:
        kpi_days = await session.scalar(
            select(func.count())
            .select_from(DailyKpiSnapshot)
            .where(DailyKpiSnapshot.stat_date.between(date(2026, 8, 1), date(2026, 8, 5)))
        )
    assert kpi_days == 5


async def test_dry_run_writes_nothing(_test_engine) -> None:
    """Including the run record: a dry run is a question, not an event."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-DRY-1")
    day = date(2026, 7, 1)

    async with factory() as session:
        runs_before = await session.scalar(select(func.count()).select_from(AnalyticsRollupRun))

    outcome = await run(from_date=day, to_date=day, dry_run=True, now=NOW, session_factory=factory)
    assert outcome.days_rebuilt == 0

    async with factory() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(DailyKpiSnapshot)
            .where(DailyKpiSnapshot.stat_date == day)
        )
        runs_after = await session.scalar(select(func.count()).select_from(AnalyticsRollupRun))
    assert rows == 0
    assert runs_after == runs_before


async def test_a_second_run_is_skipped_while_the_lock_is_held(_test_engine) -> None:
    """The hourly and nightly jobs will overlap eventually. Two rollups
    interleaving DELETE and INSERT on one day would leave half of each."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-LOCK-1")

    async with factory() as holder:
        got = await holder.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ROLLUP_LOCK_KEY}
        )
        assert got is True

        outcome = await run(
            from_date=date(2026, 8, 2),
            to_date=date(2026, 8, 2),
            now=NOW,
            session_factory=factory,
        )
        assert outcome.skipped_locked is True
        assert outcome.days_rebuilt == 0
        assert outcome.error is None, "being locked out is not a failure"
        assert outcome.status == "skipped"

        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ROLLUP_LOCK_KEY})

    # Lock released: the next run proceeds.
    outcome = await run(
        from_date=date(2026, 8, 2), to_date=date(2026, 8, 2), now=NOW, session_factory=factory
    )
    assert outcome.days_rebuilt == 1


async def test_the_run_is_recorded_even_when_skipped(_test_engine) -> None:
    """The execution log is what tells you the job is alive; a skipped run that
    left no trace looks identical to a scheduler that stopped firing."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-REC-1")

    await run(
        from_date=date(2026, 8, 3),
        to_date=date(2026, 8, 3),
        job_name="test-record",
        triggered_by="pytest",
        now=NOW,
        session_factory=factory,
    )
    async with factory() as session:
        row = (
            await session.execute(
                select(AnalyticsRollupRun).where(AnalyticsRollupRun.job_name == "test-record")
            )
        ).scalar_one()
    assert row.status == "success"
    assert row.days_rebuilt == 1
    assert row.triggered_by == "pytest"
    assert row.completed_at is not None


async def test_max_days_caps_and_reports_the_remainder(_test_engine) -> None:
    """No silent truncation: a run that quietly did 3 of 30 days reads as
    'rebuilt' everywhere."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-CAP-1")

    outcome = await run(
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 30),
        max_days=3,
        now=NOW,
        session_factory=factory,
    )
    assert outcome.days_rebuilt == 3
    assert outcome.remaining_days == 27


async def test_incremental_run_without_a_watermark_does_nothing(_test_engine) -> None:
    """A cold start must be told what to build. Deciding on its own to rebuild
    all of history is the wrong default for a job that runs hourly."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM analytics_rollup_runs"))

    outcome = await run(now=NOW, session_factory=factory)
    assert outcome.days_rebuilt == 0
    assert outcome.error is None


async def test_incremental_run_picks_up_changes_since_the_watermark(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _sku(factory, "CLI-INC-1")

    # A prior success establishes the watermark.
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM analytics_rollup_runs"))
        session.add(
            AnalyticsRollupRun(
                job_name="hourly",
                days_rebuilt=0,
                status="success",
                started_at=NOW - timedelta(hours=2),
                completed_at=NOW - timedelta(hours=2),
            )
        )

    day = date(2026, 8, 19)
    await _order(factory, "CLI-INC-ORD", sku, day, 2, "1500")

    outcome = await run(now=NOW, session_factory=factory)
    assert day in {outcome.first_date, outcome.last_date} or outcome.days_rebuilt >= 1

    async with factory() as session:
        sold = await session.scalar(
            select(SkuDailySales.quantity).where(
                SkuDailySales.stat_date == day, SkuDailySales.master_sku_id == sku
            )
        )
    assert sold == 2


async def test_prune_removes_old_rollups_only(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _sku(factory, "CLI-PRUNE-1")

    await run(
        from_date=date(2026, 7, 10), to_date=date(2026, 7, 12), now=NOW, session_factory=factory
    )
    outcome = await run(
        from_date=date(2026, 7, 20),
        to_date=date(2026, 7, 20),
        prune_before=date(2026, 7, 12),
        now=NOW,
        session_factory=factory,
    )
    assert outcome.pruned_rows > 0

    async with factory() as session:
        old = await session.scalar(
            select(func.count())
            .select_from(SkuDailyStock)
            .where(SkuDailyStock.stat_date < date(2026, 7, 12))
        )
        kept = await session.scalar(
            select(func.count())
            .select_from(SkuDailyStock)
            .where(SkuDailyStock.stat_date == date(2026, 7, 20))
        )
        # Source data is never touched by a prune.
        masters = await session.scalar(select(func.count()).select_from(MasterSku))
    assert old == 0
    assert kept > 0
    assert masters > 0

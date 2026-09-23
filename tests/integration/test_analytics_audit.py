"""検収突合の中身 (P2-042) — 受注データ側の再計算をDBに対して検証する。

`recompute_from_orders` is the second opinion the whole sheet rests on. If its
SQL is wrong it does not fail loudly; it quietly agrees with the rollup, or
quietly disagrees with it forever, and either way the 検収 conversation is
about the wrong thing. So each rule in 指標定義書 gets a row of data that would
break if the rule were dropped.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.models import (
    DailyKpiSnapshot,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services.analytics_audit import (
    missing_rollup_days,
    recompute_from_orders,
    reconcile_period,
)
from app.services.analytics_rollup import AnalyticsRollupService
from app.services.timeframe import Period

pytestmark = pytest.mark.integration

DAY = date(2026, 9, 15)
#: 12:00 JST on DAY.
NOON = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
WEEK = Period(date(2026, 9, 15), date(2026, 9, 21), "custom")
ONE_DAY = Period(DAY, DAY, "day")


async def _sku(session, code: str) -> MasterSku:
    sku = MasterSku(sku_code=code, name=code)
    session.add(sku)
    await session.flush()
    return sku


async def _order(
    session,
    order_id: str,
    *,
    ordered_at: datetime = NOON,
    status: str = OrderStatusEnum.CONFIRMED,
    lines: list[tuple[int | None, int, str]],
) -> Order:
    order = Order(
        channel="shopify",
        channel_order_id=order_id,
        status=status,
        ordered_at=ordered_at,
    )
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


# --- 定義どおりに数えているか ----------------------------------------------


async def test_sales_are_quantity_times_price_over_mapped_lines(db_session) -> None:
    sku = await _sku(db_session, "A1")
    await _order(db_session, "O-1", lines=[(sku.id, 3, "1200")])

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.gross_sales_jpy == Decimal("3600")
    assert raw.sold_quantity == 3


async def test_a_cancelled_order_contributes_no_sales(db_session) -> None:
    """And is reported separately, because it is the population that can move
    a past day's total retroactively."""
    sku = await _sku(db_session, "A2")
    await _order(db_session, "O-2", status="cancelled", lines=[(sku.id, 3, "1200")])

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.gross_sales_jpy == Decimal("0")
    assert raw.sold_quantity == 0
    assert raw.cancelled_sales_jpy == Decimal("3600")
    assert raw.cancelled_quantity == 3


async def test_a_returned_order_is_treated_like_a_cancelled_one(db_session) -> None:
    sku = await _sku(db_session, "A3")
    await _order(db_session, "O-3", status="returned", lines=[(sku.id, 1, "500")])

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.gross_sales_jpy == Decimal("0")
    assert raw.cancelled_sales_jpy == Decimal("500")


async def test_unmapped_lines_are_carried_separately_never_dropped(db_session) -> None:
    """They are real revenue whose SKU we could not resolve. Dropping them
    makes the channel shares disagree with the total."""
    sku = await _sku(db_session, "A4")
    await _order(db_session, "O-4", lines=[(sku.id, 1, "1000"), (None, 2, "700")])

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.gross_sales_jpy == Decimal("1000")
    assert raw.unmapped_sales_jpy == Decimal("1400")
    assert raw.unmapped_line_count == 1


async def test_an_order_with_three_lines_counts_once(db_session) -> None:
    """受注件数 counts orders. Summing a per-line count would treat one basket
    as three, and the client's own count is of orders."""
    sku = await _sku(db_session, "A5")
    await _order(
        db_session,
        "O-5",
        lines=[(sku.id, 1, "100"), (sku.id, 1, "100"), (sku.id, 1, "100")],
    )

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.order_count == 1
    assert raw.sold_quantity == 3


async def test_a_cancelled_order_is_not_counted_as_an_order(db_session) -> None:
    sku = await _sku(db_session, "A6")
    await _order(db_session, "O-6a", lines=[(sku.id, 1, "100")])
    await _order(db_session, "O-6b", status="cancelled", lines=[(sku.id, 1, "100")])

    raw = await recompute_from_orders(db_session, ONE_DAY)

    assert raw.order_count == 1


# --- JST で切っているか ----------------------------------------------------


async def test_an_order_just_after_jst_midnight_belongs_to_the_new_day(db_session) -> None:
    """00:30 JST on the 15th is 15:30 UTC on the 14th. Bucketing on the UTC
    date misfiles roughly a third of every day's volume."""
    sku = await _sku(db_session, "B1")
    await _order(
        db_session,
        "O-JST",
        ordered_at=datetime(2026, 9, 14, 15, 30, tzinfo=UTC),
        lines=[(sku.id, 1, "800")],
    )

    assert (await recompute_from_orders(db_session, ONE_DAY)).gross_sales_jpy == Decimal("800")
    yesterday = Period(date(2026, 9, 14), date(2026, 9, 14), "day")
    assert (await recompute_from_orders(db_session, yesterday)).gross_sales_jpy == Decimal("0")


async def test_an_order_just_before_jst_midnight_stays_on_the_old_day(db_session) -> None:
    sku = await _sku(db_session, "B2")
    await _order(
        db_session,
        "O-LATE",
        ordered_at=datetime(2026, 9, 15, 14, 59, tzinfo=UTC),  # 23:59 JST on the 15th
        lines=[(sku.id, 1, "800")],
    )

    assert (await recompute_from_orders(db_session, ONE_DAY)).gross_sales_jpy == Decimal("800")


# --- 突合そのもの ----------------------------------------------------------


async def test_a_freshly_rebuilt_day_reconciles(db_session) -> None:
    """The baseline. If this ever fails, the two paths have drifted apart and
    every other result here is meaningless."""
    sku = await _sku(db_session, "C1")
    await _order(db_session, "O-C1", lines=[(sku.id, 2, "1500"), (None, 1, "400")])
    await AnalyticsRollupService(db_session).rebuild_day(DAY)

    result = await reconcile_period(db_session, ONE_DAY)

    assert result.agrees, [(m.label, m.difference) for m in result.disagreements]
    assert result.raw.gross_sales_jpy == Decimal("3000")
    assert result.kpis.gross_sales_jpy == Decimal("3000")


async def test_a_backdated_cancellation_shows_up_as_a_disagreement(db_session) -> None:
    """The defect this harness exists to catch. The rollup for a past day was
    correct when it ran; cancelling that order afterwards changes what the day
    should say, and a rollup that only revisits recent days never returns."""
    sku = await _sku(db_session, "C2")
    order = await _order(db_session, "O-C2", lines=[(sku.id, 2, "1500")])
    await AnalyticsRollupService(db_session).rebuild_day(DAY)

    order.status = "cancelled"
    await db_session.flush()

    result = await reconcile_period(db_session, ONE_DAY)

    assert not result.agrees
    sales = next(m for m in result.measures if "売上金額" in m.label)
    assert sales.from_screen == Decimal("3000")  # what the stale rollup still says
    assert sales.from_orders == Decimal("0")  # what the orders now say
    assert result.raw.cancelled_sales_jpy == Decimal("3000")


async def test_rebuilding_the_day_clears_the_disagreement(db_session) -> None:
    """And the fix the report points at actually works."""
    sku = await _sku(db_session, "C3")
    order = await _order(db_session, "O-C3", lines=[(sku.id, 2, "1500")])
    await AnalyticsRollupService(db_session).rebuild_day(DAY)
    order.status = "cancelled"
    await db_session.flush()
    assert not (await reconcile_period(db_session, ONE_DAY)).agrees

    await AnalyticsRollupService(db_session).rebuild_day(DAY)

    assert (await reconcile_period(db_session, ONE_DAY)).agrees


# --- 集計行の欠落 ----------------------------------------------------------


async def test_days_without_a_rollup_row_are_named(db_session) -> None:
    db_session.add(
        DailyKpiSnapshot(
            stat_date=DAY,
            total_on_hand_qty=0,
            sku_count=0,
            out_of_stock_sku_count=0,
            negative_stock_sku_count=0,
            order_count=0,
            sold_quantity=0,
            gross_sales_jpy=Decimal("0"),
            unmapped_sales_jpy=Decimal("0"),
        )
    )
    await db_session.flush()

    missing = await missing_rollup_days(db_session, WEEK)

    assert DAY not in missing
    assert len(missing) == WEEK.days - 1
    assert date(2026, 9, 21) in missing


async def test_a_fully_covered_window_reports_no_gap(db_session) -> None:
    for offset in range(WEEK.days):
        await AnalyticsRollupService(db_session).rebuild_day(
            date(2026, 9, 15 + offset),
        )

    assert await missing_rollup_days(db_session, WEEK) == []

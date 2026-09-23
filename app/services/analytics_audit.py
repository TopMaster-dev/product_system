"""検収: do the screen's numbers still equal the orders they came from? (P2-042)

The client will take a week of real data, add it up by hand from the order
list, and compare it against the dashboard. This is that check, automated, so
the disagreement is found here rather than in the 検収 meeting.

WHY A SECOND IMPLEMENTATION IS THE POINT

`analytics_query` reads `daily_kpi_snapshots`, which `analytics_rollup` wrote
one day at a time. This recomputes the same totals from `orders` and
`order_items` in a single pass over the whole period. Calling the rollup's own
helpers would prove only that a function equals itself; the definitions below
are written out from 指標定義書 (docs/22) instead, and that is deliberate.

WHAT A DISAGREEMENT MEANS

The two paths can only differ for reasons worth knowing about:

* **A day never got a rollup row.** The window reaches back before the job ran,
  or a run failed. Reported as `missing_days`.
* **A day's row is older than the orders it covers.** The known one:
  cancelling a 60-day-old order rewrites that day's totals, and a rollup that
  only revisits recent days never returns to fix it. `cancelled_sales_jpy`
  is reported for exactly this reason — it is the size of the population that
  can move retroactively.
* **The rollup has not caught up yet.** Ordinary lag on the current day.

Definitions reproduced here, from docs/22:

* Bucketed on `orders.ordered_at` converted to JST — the day the sale was
  booked, not the day it was touched.
* Cancelled and returned orders contribute nothing to sales or quantity.
* `order_count` counts DISTINCT orders, not lines: a three-line order is one.
* Lines with no `master_sku_id` are unmapped revenue. They are counted
  separately and never dropped, or the channel shares stop summing to the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyKpiSnapshot, Order, OrderItem
from app.services.analytics_query import Kpis, period_kpis
from app.services.timeframe import Period

#: Reproduced from docs/22 rather than imported, for the reason in the module
#: docstring. If this ever disagrees with `analytics_rollup.CANCELLED_STATUSES`,
#: one of the two is wrong and the audit is what says so.
CANCELLED_STATUSES = ("cancelled", "returned")


@dataclass(frozen=True, slots=True)
class RawTotals:
    """The period, added up straight from the orders."""

    gross_sales_jpy: Decimal
    sold_quantity: int
    order_count: int
    unmapped_sales_jpy: Decimal
    #: Not a KPI. The size of what can still move retroactively.
    cancelled_sales_jpy: Decimal
    cancelled_quantity: int
    unmapped_line_count: int


@dataclass(frozen=True, slots=True)
class Measure:
    """One number, from both paths."""

    label: str
    from_screen: Decimal
    from_orders: Decimal

    @property
    def difference(self) -> Decimal:
        return self.from_screen - self.from_orders

    @property
    def agrees(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True, slots=True)
class Reconciliation:
    period: Period
    kpis: Kpis
    raw: RawTotals
    measures: list[Measure]
    #: JST days in the window with no `daily_kpi_snapshots` row at all.
    missing_days: list[date]

    @property
    def agrees(self) -> bool:
        return all(m.agrees for m in self.measures)

    @property
    def disagreements(self) -> list[Measure]:
        return [m for m in self.measures if not m.agrees]


async def recompute_from_orders(session: AsyncSession, period: Period) -> RawTotals:
    """The hand calculation, in SQL. Touches no rollup table."""
    start, end = period.utc_bounds()
    in_period = (Order.ordered_at >= start, Order.ordered_at < end)
    cancelled = Order.status.in_(CANCELLED_STATUSES)
    live = ~cancelled
    mapped = OrderItem.master_sku_id.is_not(None)
    amount = OrderItem.quantity * OrderItem.unit_price

    lines = (
        await session.execute(
            select(
                func.coalesce(func.sum(amount).filter(live, mapped), 0),
                func.coalesce(func.sum(OrderItem.quantity).filter(live, mapped), 0),
                func.coalesce(func.sum(amount).filter(live, ~mapped), 0),
                func.coalesce(func.sum(amount).filter(cancelled), 0),
                func.coalesce(func.sum(OrderItem.quantity).filter(cancelled), 0),
                func.count().filter(~mapped),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(*in_period)
        )
    ).one()

    # Distinct orders, counted on the orders table — summing a per-line count
    # would count a three-line order three times.
    order_count = (
        await session.scalar(
            select(func.count(func.distinct(Order.id))).where(
                *in_period,
                Order.status.not_in(CANCELLED_STATUSES),
            )
        )
        or 0
    )

    return RawTotals(
        gross_sales_jpy=Decimal(lines[0]),
        sold_quantity=int(lines[1]),
        unmapped_sales_jpy=Decimal(lines[2]),
        cancelled_sales_jpy=Decimal(lines[3]),
        cancelled_quantity=int(lines[4]),
        unmapped_line_count=int(lines[5]),
        order_count=int(order_count),
    )


async def missing_rollup_days(session: AsyncSession, period: Period) -> list[date]:
    """Days in the window that produced no KPI row.

    Named rather than inferred from a count: "26 rows for a 28-day window" does
    not say WHICH two are missing, and the two dates are what makes the gap
    explainable to the client.
    """
    rows = await session.execute(
        select(DailyKpiSnapshot.stat_date).where(
            DailyKpiSnapshot.stat_date >= period.first_day,
            DailyKpiSnapshot.stat_date <= period.last_day,
        )
    )
    present = set(rows.scalars().all())
    window = (period.first_day + timedelta(days=i) for i in range(period.days))
    return [day for day in window if day not in present]


async def reconcile_period(session: AsyncSession, period: Period) -> Reconciliation:
    """Both paths, side by side."""
    kpis = await period_kpis(session, period)
    raw = await recompute_from_orders(session, period)

    measures = [
        Measure("売上金額 (マッピング済)", kpis.gross_sales_jpy, raw.gross_sales_jpy),
        Measure("販売点数", Decimal(kpis.sold_quantity), Decimal(raw.sold_quantity)),
        Measure("受注件数", Decimal(kpis.order_count), Decimal(raw.order_count)),
        Measure("未マッピング売上", kpis.unmapped_sales_jpy, raw.unmapped_sales_jpy),
    ]
    return Reconciliation(
        period=period,
        kpis=kpis,
        raw=raw,
        measures=measures,
        missing_days=await missing_rollup_days(session, period),
    )


__all__ = [
    "CANCELLED_STATUSES",
    "Measure",
    "RawTotals",
    "Reconciliation",
    "missing_rollup_days",
    "recompute_from_orders",
    "reconcile_period",
]

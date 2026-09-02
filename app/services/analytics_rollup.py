"""AnalyticsRollupService — builds the daily rollups every screen reads.

WHICH DAYS GET REBUILT
----------------------
Not a fixed trailing window. The rebuild set is derived from what actually
changed:

    days = { JST(occurred_at) for events created since the last success }
      UNION { JST(ordered_at)  for orders updated since the last success }

The distinction between `created_at` and `occurred_at` is the whole point. A
cancellation of a 60-day-old order writes an event whose **occurred_at** is
recent but whose **order** is two months back; and before the 2026-08-20 fix,
compensation events were stamped with the ORIGINAL ORDER's time, so production
holds events whose occurred_at is months earlier than their created_at. The
event log's 遡及 badge exists because those rows are real.

A fixed 3-day window cannot see either case. It would silently leave the
affected day wrong forever, because nothing ever revisits it — and a forward
running sum never notices.

WHICH SKUs COUNT — TWO DIFFERENT RULES
--------------------------------------
`sku_daily_stock` is CURRENT STATE, so it uses
`analysable_conditions(include_archived=False)`: no bundle parents (their stock
is derived from components, and counting both double-counts the fleet), no
在庫管理対象外, no archived.

`sku_daily_sales` is HISTORY, and takes **every mapped order line**, with no
population filter at all. Two reasons:

* An archived SKU really did sell before it was retired. Dropping its rows
  would erase that revenue from every period comparison spanning the cutover —
  the exact asymmetry `sku_scope` was written to prevent.
* Filtering here would break the arithmetic. `sum(sku_daily_sales) +
  sum(daily_unmapped_sales)` must equal the shop's real revenue, or the category
  totals stop adding up to the headline figure and nobody can tell which number
  is wrong. Screens narrow the population at READ time instead, where the
  filtering is visible and reversible.

Sales attach to the ORDERED sku — a bundle parent, not its components. One set
sold is one sale; counting the components would inflate revenue. Stock is the
mirror image: the components are what moved.

Sales are bucketed by `orders.ordered_at`, so cancelling an old order corrects
the day the sale was originally booked. That is what makes the data-driven
window necessary rather than merely nicer.

The service does NOT commit. The caller owns a transaction PER DAY, so a failure
midway through a 90-day backfill leaves every completed day durable — the same
reasoning as the windowed BigQuery export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DailyKpiSnapshot,
    DailyUnmappedSales,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
    SkuDailySales,
    SkuDailyStock,
)
from app.services.sku_scope import analysable_conditions
from app.services.timeframe import jst_date_expr, jst_day_bounds, to_jst_date

#: Order statuses that mean the sale did not stick.
CANCELLED_STATUSES = ("cancelled", "returned")


@dataclass(slots=True)
class DayResult:
    stat_date: date
    stock_rows: int = 0
    sales_rows: int = 0
    unmapped_rows: int = 0


@dataclass(slots=True)
class RollupResult:
    days: list[DayResult] = field(default_factory=list)

    @property
    def days_rebuilt(self) -> int:
        return len(self.days)

    @property
    def first_date(self) -> date | None:
        return min((d.stat_date for d in self.days), default=None)

    @property
    def last_date(self) -> date | None:
        return max((d.stat_date for d in self.days), default=None)


def walk_daily_balances(
    opening: dict[int, int],
    deltas: dict[tuple[date, int], int],
    days: list[date],
    population: list[int],
) -> dict[tuple[date, int], int]:
    """Closing balance per (day, SKU), carrying yesterday forward.

    Pure, and separated out because it is the part most likely to be wrong.
    Two properties it has to hold:

    * **Dense.** Every SKU gets a row every day, movement or not — stock is a
      state, and a sparse grid would push gap-filling into every chart.
    * **Carried forward.** A SKU that did not move keeps yesterday's balance
      rather than resetting to zero. Recomputing each day from scratch would be
      correct but O(days x history); this is one pass.
    """
    balances = dict(opening)
    out: dict[tuple[date, int], int] = {}
    for day in days:
        for sku_id in population:
            balances[sku_id] = balances.get(sku_id, 0) + deltas.get((day, sku_id), 0)
            out[(day, sku_id)] = balances[sku_id]
    return out


class AnalyticsRollupService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- which days ----------

    async def dates_to_rebuild(self, since: datetime | None) -> list[date]:
        """JST days touched by anything created/updated since `since`.

        `since=None` (no successful run yet) returns [] — a first run has no
        delta to compute and must be given an explicit range instead, so a
        cold start cannot silently decide to rebuild all of history.
        """
        if since is None:
            return []

        event_days = select(jst_date_expr(InventoryEvent.occurred_at).label("d")).where(
            InventoryEvent.created_at > since
        )
        order_days = select(jst_date_expr(Order.ordered_at).label("d")).where(
            Order.updated_at > since
        )
        rows = await self._session.execute(event_days.union(order_days))
        return sorted({d for (d,) in rows.all() if d is not None})

    # ---------- one day ----------

    async def rebuild_day(self, day: date, *, measure_drift: bool = False) -> DayResult:
        """DELETE then INSERT every rollup row for one JST day.

        DELETE+INSERT rather than upsert: a rebuild must be able to REMOVE rows
        that should no longer exist (a SKU that left the population, a sale that
        was moved), and an upsert would leave those behind forever.
        """
        result = DayResult(stat_date=day)
        await self._clear_day(day)

        population = await self._stock_population()
        result.stock_rows = await self._build_stock(day, population)
        result.sales_rows = await self._build_sales(day)
        result.unmapped_rows = await self._build_unmapped(day)
        await self._build_kpi(day, measure_drift=measure_drift)
        return result

    async def _clear_day(self, day: date) -> None:
        for model in (SkuDailyStock, SkuDailySales, DailyUnmappedSales, DailyKpiSnapshot):
            await self._session.execute(delete(model).where(model.stat_date == day))

    async def _stock_population(self) -> list[int]:
        rows = await self._session.execute(
            select(MasterSku.id)
            .where(*analysable_conditions(include_archived=False))
            .order_by(MasterSku.id)
        )
        return [i for (i,) in rows.all()]

    async def _build_stock(self, day: date, population: list[int]) -> int:
        if not population:
            return 0
        start, end = jst_day_bounds(day)

        opening_rows = await self._session.execute(
            select(
                InventoryEvent.master_sku_id,
                func.coalesce(func.sum(InventoryEvent.quantity_delta), 0),
            )
            .where(
                InventoryEvent.occurred_at < start,
                InventoryEvent.master_sku_id.in_(population),
            )
            .group_by(InventoryEvent.master_sku_id)
        )
        opening = {sku: int(total) for sku, total in opening_rows.all()}

        moved = await self._session.execute(
            select(
                InventoryEvent.master_sku_id,
                func.coalesce(func.sum(InventoryEvent.quantity_delta), 0),
                # FILTER belongs to the AGGREGATE, not to abs(): Postgres
                # rejects `abs(x) FILTER (...)` with "not an aggregate function".
                func.coalesce(
                    func.sum(func.abs(InventoryEvent.quantity_delta)).filter(
                        InventoryEvent.event_type == InventoryEventTypeEnum.ORDER_CONSUMED
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(func.abs(InventoryEvent.quantity_delta)).filter(
                        InventoryEvent.event_type == InventoryEventTypeEnum.CANCELLATION_RETURNED
                    ),
                    0,
                ),
            )
            .where(
                InventoryEvent.occurred_at >= start,
                InventoryEvent.occurred_at < end,
                InventoryEvent.master_sku_id.in_(population),
            )
            .group_by(InventoryEvent.master_sku_id)
        )
        deltas: dict[tuple[date, int], int] = {}
        consumed: dict[int, int] = {}
        returned: dict[int, int] = {}
        for sku, net, used, back in moved.all():
            deltas[(day, sku)] = int(net)
            consumed[sku] = int(used or 0)
            returned[sku] = int(back or 0)

        balances = walk_daily_balances(opening, deltas, [day], population)
        self._session.add_all(
            [
                SkuDailyStock(
                    stat_date=day,
                    master_sku_id=sku,
                    on_hand_qty=balances[(day, sku)],
                    consumed_qty=consumed.get(sku, 0),
                    returned_qty=returned.get(sku, 0),
                )
                for sku in population
            ]
        )
        return len(population)

    def _lines_for_day(self, day: date) -> Select[tuple]:  # type: ignore[type-arg]
        start, end = jst_day_bounds(day)
        # Bucketed on ordered_at, so cancelling an old order corrects the day
        # the sale was originally booked rather than today.
        return (
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.ordered_at >= start, Order.ordered_at < end)
        )

    async def _build_sales(self, day: date) -> int:
        start, end = jst_day_bounds(day)
        cancelled = Order.status.in_(CANCELLED_STATUSES)
        amount = OrderItem.quantity * OrderItem.unit_price

        rows = await self._session.execute(
            select(
                Order.channel,
                OrderItem.master_sku_id,
                MasterSku.category_id,
                func.count(func.distinct(Order.id)),
                func.coalesce(func.sum(OrderItem.quantity).filter(~cancelled), 0),
                func.coalesce(func.sum(amount).filter(~cancelled), 0),
                func.coalesce(func.sum(OrderItem.quantity).filter(cancelled), 0),
                func.coalesce(func.sum(amount).filter(cancelled), 0),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .join(MasterSku, MasterSku.id == OrderItem.master_sku_id)
            .where(
                Order.ordered_at >= start,
                Order.ordered_at < end,
                OrderItem.master_sku_id.is_not(None),
            )
            .group_by(Order.channel, OrderItem.master_sku_id, MasterSku.category_id)
        )
        added = 0
        for channel, sku, category, orders, qty, gross, cq, cs in rows.all():
            self._session.add(
                SkuDailySales(
                    stat_date=day,
                    channel=channel,
                    master_sku_id=sku,
                    category_id=category,
                    order_count=int(orders),
                    quantity=int(qty),
                    gross_sales_jpy=Decimal(gross),
                    cancelled_quantity=int(cq),
                    cancelled_sales_jpy=Decimal(cs),
                )
            )
            added += 1
        return added

    async def _build_unmapped(self, day: date) -> int:
        """Lines that never resolved to a master. Kept, never dropped — see the
        module docstring on why the arithmetic depends on it."""
        start, end = jst_day_bounds(day)
        amount = OrderItem.quantity * OrderItem.unit_price
        rows = await self._session.execute(
            select(
                Order.channel,
                OrderItem.channel_sku,
                func.count(func.distinct(Order.id)),
                func.coalesce(func.sum(OrderItem.quantity), 0),
                func.coalesce(func.sum(amount), 0),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.ordered_at >= start,
                Order.ordered_at < end,
                OrderItem.master_sku_id.is_(None),
                Order.status.not_in(CANCELLED_STATUSES),
            )
            .group_by(Order.channel, OrderItem.channel_sku)
        )
        added = 0
        for channel, channel_sku, orders, qty, gross in rows.all():
            self._session.add(
                DailyUnmappedSales(
                    stat_date=day,
                    channel=channel,
                    channel_sku=channel_sku,
                    order_count=int(orders),
                    quantity=int(qty),
                    gross_sales_jpy=Decimal(gross),
                )
            )
            added += 1
        return added

    async def _build_kpi(self, day: date, *, measure_drift: bool) -> None:
        await self._session.flush()  # the day's rows must be visible to these aggregates
        start, end = jst_day_bounds(day)

        stock = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(SkuDailyStock.on_hand_qty), 0),
                    func.count(),
                    func.count().filter(SkuDailyStock.on_hand_qty == 0),
                    func.count().filter(SkuDailyStock.on_hand_qty < 0),
                ).where(SkuDailyStock.stat_date == day)
            )
        ).one()

        sales = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(SkuDailySales.quantity), 0),
                    func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
                ).where(SkuDailySales.stat_date == day)
            )
        ).one()

        # Counted from orders directly, NOT by summing sku_daily_sales.order_count
        # — an order with three lines would otherwise count three times.
        order_count = (
            await self._session.scalar(
                select(func.count(func.distinct(Order.id))).where(
                    Order.ordered_at >= start,
                    Order.ordered_at < end,
                    Order.status.not_in(CANCELLED_STATUSES),
                )
            )
            or 0
        )

        unmapped = await self._session.scalar(
            select(func.coalesce(func.sum(DailyUnmappedSales.gross_sales_jpy), 0)).where(
                DailyUnmappedSales.stat_date == day
            )
        ) or Decimal("0")

        self._session.add(
            DailyKpiSnapshot(
                stat_date=day,
                total_on_hand_qty=int(stock[0]),
                sku_count=int(stock[1]),
                out_of_stock_sku_count=int(stock[2]),
                negative_stock_sku_count=int(stock[3]),
                order_count=int(order_count),
                sold_quantity=int(sales[0]),
                gross_sales_jpy=Decimal(sales[1]),
                unmapped_sales_jpy=Decimal(unmapped),
                snapshot_drift_count=(await self._snapshot_drift()) if measure_drift else None,
            )
        )

    async def _snapshot_drift(self) -> int:
        """Rollup-vs-live disagreement, measured for TODAY only.

        Historical days have nothing current to compare against, and the query
        aggregates all of `inventory_events` — running it per day across a
        90-day backfill would do the same expensive work ninety times to answer
        a question only today can be asked.
        """
        totals = (
            select(
                InventoryEvent.master_sku_id.label("mid"),
                func.coalesce(func.sum(InventoryEvent.quantity_delta), 0).label("total"),
            )
            .group_by(InventoryEvent.master_sku_id)
            .subquery()
        )
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(InventorySnapshot)
                .join(totals, totals.c.mid == InventorySnapshot.master_sku_id)
                .where(InventorySnapshot.on_hand_qty != totals.c.total)
            )
            or 0
        )

    async def last_success_at(self, job_name: str | None = None) -> datetime | None:
        """Watermark: when the last successful run finished.

        `completed_at`, not `started_at`. Using the start time would skip any
        row written while the run was in flight, losing it permanently.
        """
        from app.models import AnalyticsRollupRun

        stmt = select(func.max(AnalyticsRollupRun.completed_at)).where(
            AnalyticsRollupRun.status == "success"
        )
        if job_name:
            stmt = stmt.where(AnalyticsRollupRun.job_name == job_name)
        return await self._session.scalar(stmt)


def is_today(day: date, now: datetime) -> bool:
    """Drift is only measurable for the current JST day."""
    return day == to_jst_date(now if now.tzinfo else now.replace(tzinfo=UTC))


__all__ = [
    "CANCELLED_STATUSES",
    "AnalyticsRollupService",
    "DayResult",
    "RollupResult",
    "is_today",
    "walk_daily_balances",
]

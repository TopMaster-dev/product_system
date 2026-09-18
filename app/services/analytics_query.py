"""Reading the daily rollups back out over a period (P2-009 / P2-011 / P2-012).

`analytics_rollup.py` writes one row per SKU per JST day; this reads those rows
for a window and hands the screens finished numbers. The split matters: the
rollup is a scheduled job whose correctness is about completeness, while this is
a request-path query whose correctness is about how a period aggregates.

THE DISTINCTION EVERY FUNCTION HERE TURNS ON: FLOWS vs BALANCES
---------------------------------------------------------------
`gross_sales_jpy`, `sold_quantity` and `order_count` are FLOWS — things that
happened during a day — and a period total is their SUM.

`total_on_hand_qty`, `sku_count` and `out_of_stock_sku_count` are BALANCES —
what was true at the end of a day — and summing them is meaningless. Twenty-eight
days of "we hold 5,000 units" is not 140,000 units. A period's balance is its
LAST day's value, and the only aggregate a balance admits is an average.

Both kinds sit side by side in `daily_kpi_snapshots`, one `func.sum` apart, and
the wrong one produces a number that is merely large rather than obviously
broken — nobody reviewing a dashboard catches a stock figure that is 28x too
high, because nobody knows what it should be.

Unmapped sales are carried everywhere, never dropped. They are real revenue
whose SKU we could not resolve, and excluding them makes the channel shares
disagree with the total — see `docs/23` on why the arithmetic depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    AnalyticsRollupRun,
    DailyKpiSnapshot,
    DailyUnmappedSales,
    MasterSku,
    ProductCategory,
    SkuDailySales,
    SkuDailyStock,
)
from app.services.timeframe import Period, bucket, pct_change

#: A rollup older than this is called out on the screen. The hourly job leaves
#: at most an hour of lag in normal operation, so three hours means two
#: consecutive misses — past coincidence, and the numbers below are stale in a
#: way no reader could otherwise detect.
STALE_AFTER_HOURS = 3.0

#: Above this share of revenue, the unmapped figure stops being a footnote and
#: starts distorting every channel comparison on the page.
UNMAPPED_NOTICE_RATIO = 0.05

#: The label unmapped revenue carries in channel breakdowns. Not a channel, but
#: it has to appear as one or the parts stop summing to the whole.
UNMAPPED_LABEL = "未マッピング"


@dataclass(frozen=True, slots=True)
class Kpis:
    """Period totals. Flows are summed; balances are end-of-period."""

    gross_sales_jpy: Decimal
    sold_quantity: int
    order_count: int
    unmapped_sales_jpy: Decimal
    #: End of period, NOT a sum. See the module docstring.
    total_on_hand_qty: int
    out_of_stock_sku_count: int
    sku_count: int
    #: Mean daily closing stock across the window — the denominator turnover
    #: needs, and the only aggregate a balance legitimately has.
    average_on_hand_qty: float
    #: Days that produced a rollup row at all. A window reaching back before the
    #: system started is mostly empty, and every average here divides by THIS
    #: rather than by the window length, or the earliest periods read low.
    days_with_data: int

    @property
    def turnover(self) -> float | None:
        """Quantity-based turnover FOR THE PERIOD. Deliberately not annualised.

        Annualising a 7-day window multiplies its noise by 52 and presents the
        result as a yearly rate; the client would compare that against a 90-day
        window's and conclude the business changed. The period is stated next to
        the number on the screen instead.

        None when there is no stock to turn over — a ratio over zero is not
        infinity here, it is unanswerable.
        """
        if self.average_on_hand_qty <= 0:
            return None
        return self.sold_quantity / self.average_on_hand_qty

    @property
    def total_with_unmapped_jpy(self) -> Decimal:
        return self.gross_sales_jpy + self.unmapped_sales_jpy

    @property
    def unmapped_ratio(self) -> float:
        total = self.total_with_unmapped_jpy
        if total <= 0:
            return 0.0
        return float(self.unmapped_sales_jpy / total)

    @property
    def unmapped_is_material(self) -> bool:
        return self.unmapped_ratio > UNMAPPED_NOTICE_RATIO


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where the numbers on the page came from, and whether to trust them."""

    last_rollup_at: datetime | None
    data_start: date | None
    snapshot_drift_count: int | None
    stale_hours: float | None

    @property
    def is_stale(self) -> bool:
        return self.stale_hours is not None and self.stale_hours >= STALE_AFTER_HOURS

    @property
    def has_drift(self) -> bool:
        """The snapshot/event invariant is broken somewhere.

        Shown in red rather than hidden: it means a stock figure on this page
        disagrees with the event log it is derived from, and nothing else on the
        screen can be relied on until it is resolved.
        """
        return bool(self.snapshot_drift_count)


def _within(column: Any, period: Period) -> list[ColumnElement[bool]]:
    """The two conditions that bound a query to the window.

    `column` is typed Any for the same reason `timeframe.jst_date_expr` is:
    callers pass ORM attributes, whose comparison operators are typed for
    Python semantics rather than SQL ones.
    """
    return [column >= period.first_day, column <= period.last_day]


async def period_kpis(session: AsyncSession, period: Period) -> Kpis:
    """One row of headline numbers for the window."""
    flows = await session.execute(
        select(
            func.coalesce(func.sum(DailyKpiSnapshot.gross_sales_jpy), 0),
            func.coalesce(func.sum(DailyKpiSnapshot.sold_quantity), 0),
            func.coalesce(func.sum(DailyKpiSnapshot.order_count), 0),
            func.coalesce(func.sum(DailyKpiSnapshot.unmapped_sales_jpy), 0),
            func.coalesce(func.avg(DailyKpiSnapshot.total_on_hand_qty), 0),
            func.count(),
        ).where(*_within(DailyKpiSnapshot.stat_date, period))
    )
    sales, quantity, orders, unmapped, avg_on_hand, days = flows.one()

    # The balances come from the LAST day that has a row, which is not
    # necessarily period.last_day: the rollup may not have reached it yet, and
    # reading zero from a missing row would report the shop as empty.
    latest = await session.execute(
        select(
            DailyKpiSnapshot.total_on_hand_qty,
            DailyKpiSnapshot.out_of_stock_sku_count,
            DailyKpiSnapshot.sku_count,
        )
        .where(*_within(DailyKpiSnapshot.stat_date, period))
        .order_by(DailyKpiSnapshot.stat_date.desc())
    )
    closing = latest.first()

    return Kpis(
        gross_sales_jpy=Decimal(sales),
        sold_quantity=int(quantity),
        order_count=int(orders),
        unmapped_sales_jpy=Decimal(unmapped),
        total_on_hand_qty=int(closing[0]) if closing else 0,
        out_of_stock_sku_count=int(closing[1]) if closing else 0,
        sku_count=int(closing[2]) if closing else 0,
        average_on_hand_qty=float(avg_on_hand or 0),
        days_with_data=int(days),
    )


@dataclass(frozen=True, slots=True)
class Delta:
    """A KPI against its comparison period.

    `change` is None whenever the baseline is zero or absent, and the screen
    renders that as an em dash. Growth from nothing has no percentage, and
    showing +100% invents a figure someone would act on.
    """

    current: float
    baseline: float
    change: float | None

    @classmethod
    def of(cls, current: float | int | Decimal, baseline: float | int | Decimal) -> Delta:
        cur, base = float(current), float(baseline)
        return cls(cur, base, pct_change(cur, base))


async def daily_sales(session: AsyncSession, period: Period) -> dict[date, Decimal]:
    """Mapped revenue per JST day. Days with no sales are absent, not zero —
    the caller decides whether a gap means zero or means no data yet."""
    rows = await session.execute(
        select(
            DailyKpiSnapshot.stat_date,
            DailyKpiSnapshot.gross_sales_jpy,
        )
        .where(*_within(DailyKpiSnapshot.stat_date, period))
        .order_by(DailyKpiSnapshot.stat_date)
    )
    return {day: Decimal(value) for day, value in rows.all()}


async def channel_share(session: AsyncSession, period: Period) -> list[tuple[str, Decimal]]:
    """Revenue by channel, descending, with unmapped as its own row.

    Unmapped is appended rather than distributed or dropped. Distributing it
    would invent channel attribution we do not have; dropping it would make the
    shares sum to less than the headline total, and the two numbers sit on the
    same screen.
    """
    mapped = await session.execute(
        select(
            SkuDailySales.channel,
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
        )
        .where(*_within(SkuDailySales.stat_date, period))
        .group_by(SkuDailySales.channel)
    )
    shares = [(channel, Decimal(total)) for channel, total in mapped.all()]

    unmapped = await session.scalar(
        select(func.coalesce(func.sum(DailyUnmappedSales.gross_sales_jpy), 0)).where(
            *_within(DailyUnmappedSales.stat_date, period)
        )
    )
    if unmapped:
        shares.append((UNMAPPED_LABEL, Decimal(unmapped)))

    shares.sort(key=lambda row: row[1], reverse=True)
    return shares


async def provenance(session: AsyncSession, *, now: datetime) -> Provenance:
    """When the rollup last succeeded, how far back the data goes, and whether
    the snapshot invariant currently holds."""
    last_run = await session.scalar(
        select(func.max(AnalyticsRollupRun.completed_at)).where(
            AnalyticsRollupRun.status == "succeeded"
        )
    )
    data_start = await session.scalar(select(func.min(DailyKpiSnapshot.stat_date)))

    # Drift is read from the most recent day that measured it. The nightly
    # repair job sets it; the hourly job leaves it NULL, so `max(stat_date)`
    # alone would usually find a NULL and report "no drift" on a system that has
    # some.
    drift = await session.scalar(
        select(DailyKpiSnapshot.snapshot_drift_count)
        .where(DailyKpiSnapshot.snapshot_drift_count.is_not(None))
        .order_by(DailyKpiSnapshot.stat_date.desc())
        .limit(1)
    )

    stale_hours = None
    if last_run is not None:
        stale_hours = (now - last_run).total_seconds() / 3600.0

    return Provenance(
        last_rollup_at=last_run,
        data_start=data_start,
        snapshot_drift_count=drift,
        stale_hours=stale_hours,
    )


# ---------------------------------------------------------------------------
# 売上明細 (P2-008 / P2-010 / P2-011)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SalesFilter:
    """What the detail screen is currently narrowed to.

    `category_id` has THREE meanings, which is why it is not a plain int|None:
    a number selects that category, None means no filter, and UNCLASSIFIED
    selects the SKUs that have no category at all. Collapsing the last two
    would hide exactly the rows the client needs to find while they are still
    filling the category sheet in.
    """

    channel: str | None = None
    category_id: int | None = None
    unclassified_only: bool = False

    def conditions(self) -> list[ColumnElement[bool]]:
        out = self.conditions_except_category()
        if self.unclassified_only:
            out.append(SkuDailySales.category_id.is_(None))
        elif self.category_id is not None:
            out.append(SkuDailySales.category_id == self.category_id)
        return out

    def conditions_except_category(self) -> list[ColumnElement[bool]]:
        """Everything but the category narrowing.

        The category breakdown applies its own grouping on that column, so it
        needs the rest of the filter without it. Stated as a method rather than
        picked apart at the call site: sifting rendered SQL for the word
        "category_id" works until a column is renamed.
        """
        out: list[ColumnElement[bool]] = []
        if self.channel:
            out.append(SkuDailySales.channel == self.channel)
        return out

    @property
    def is_narrowed(self) -> bool:
        return bool(self.channel) or self.unclassified_only or self.category_id is not None


@dataclass(frozen=True, slots=True)
class BucketRow:
    bucket: date
    quantity: int
    gross_sales_jpy: Decimal
    order_count: int


async def bucketed_sales(
    session: AsyncSession,
    period: Period,
    *,
    granularity: str = "day",
    where: SalesFilter | None = None,
) -> list[BucketRow]:
    """Sales grouped into day / week / month buckets.

    Bucketing happens in PYTHON, not SQL. `date_trunc('week', ...)` is ISO
    Monday-based in Postgres, which happens to match, but the same expression
    over a `stat_date` that is already a JST calendar date would be silently
    re-interpreted if the column type ever changed. `timeframe.bucket` is the
    one definition of a week in this system and it is unit-tested; going through
    it keeps the screens and the exports agreeing.

    Day counts here are small — a 365-day window is 365 rows before grouping.
    """
    where = where or SalesFilter()
    rows = await session.execute(
        select(
            SkuDailySales.stat_date,
            func.coalesce(func.sum(SkuDailySales.quantity), 0),
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
            func.coalesce(func.sum(SkuDailySales.order_count), 0),
        )
        .where(*_within(SkuDailySales.stat_date, period), *where.conditions())
        .group_by(SkuDailySales.stat_date)
        .order_by(SkuDailySales.stat_date)
    )

    totals: dict[date, list[Any]] = {}
    for stat_date, quantity, sales, orders in rows.all():
        key = bucket(stat_date, granularity)
        acc = totals.setdefault(key, [0, Decimal(0), 0])
        acc[0] += int(quantity)
        acc[1] += Decimal(sales)
        acc[2] += int(orders)

    return [
        BucketRow(bucket=key, quantity=v[0], gross_sales_jpy=v[1], order_count=v[2])
        for key, v in sorted(totals.items())
    ]


@dataclass(frozen=True, slots=True)
class SkuRow:
    master_sku_id: int
    sku_code: str
    name: str
    category_name: str | None
    quantity: int
    gross_sales_jpy: Decimal


async def top_skus(
    session: AsyncSession,
    period: Period,
    *,
    limit: int | None = 50,
    where: SalesFilter | None = None,
) -> list[SkuRow]:
    """Best sellers for the window, by revenue.

    Ordered by revenue rather than quantity: a 300-yen packaging line outsells
    every necklace by unit count, and a ranking headed by gift boxes tells the
    client nothing they can act on.

    `limit=None` returns every row, which is what the CSV export uses. A screen
    showing the top 50 is a deliberate summary; an export that silently stopped
    at 50 would be a subset the recipient had no way to detect, and they would
    sum it.
    """
    where = where or SalesFilter()
    stmt = (
        select(
            SkuDailySales.master_sku_id,
            MasterSku.sku_code,
            MasterSku.name,
            ProductCategory.name,
            func.coalesce(func.sum(SkuDailySales.quantity), 0),
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
        )
        .join(MasterSku, MasterSku.id == SkuDailySales.master_sku_id)
        .outerjoin(ProductCategory, ProductCategory.id == SkuDailySales.category_id)
        .where(*_within(SkuDailySales.stat_date, period), *where.conditions())
        .group_by(
            SkuDailySales.master_sku_id,
            MasterSku.sku_code,
            MasterSku.name,
            ProductCategory.name,
        )
        .order_by(func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0).desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await session.execute(stmt)
    return [
        SkuRow(
            master_sku_id=master_id,
            sku_code=code,
            name=name,
            category_name=category,
            quantity=int(quantity),
            gross_sales_jpy=Decimal(sales),
        )
        for master_id, code, name, category, quantity, sales in rows.all()
    ]


async def channels_in_period(session: AsyncSession, period: Period) -> list[str]:
    """The channels that actually traded, for the filter control.

    Read from the data rather than from ChannelEnum: offering a channel that
    produced nothing gives an empty screen and no explanation. Wholesale in
    particular is defined in the enum but has no sales until November 2026.
    """
    rows = await session.execute(
        select(SkuDailySales.channel)
        .where(*_within(SkuDailySales.stat_date, period))
        .group_by(SkuDailySales.channel)
        .order_by(SkuDailySales.channel)
    )
    return [c for (c,) in rows.all()]


# ---------------------------------------------------------------------------
# SKU別の推移 (P2-007 / P2-008)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkuDay:
    """One SKU on one JST day. Stock is a closing balance; the rest are flows."""

    stat_date: date
    on_hand_qty: int | None
    consumed_qty: int
    quantity: int
    gross_sales_jpy: Decimal


@dataclass(frozen=True, slots=True)
class SkuProfile:
    """Why a SKU's stock chart may legitimately be empty.

    `sku_daily_stock` only covers the analysable population — no set parents, no
    非在庫 items, nothing archived. For those, an empty chart is CORRECT and
    reads as "this SKU held nothing", which is the opposite of the truth for a
    set parent whose availability is derived from its components.

    So the screen states the reason instead of drawing a flat line at zero.
    """

    master_sku_id: int
    sku_code: str
    name: str
    is_bundle: bool
    is_stock_managed: bool
    archived_at: datetime | None
    category_name: str | None

    @property
    def tracks_stock(self) -> bool:
        return not self.is_bundle and self.is_stock_managed

    @property
    def no_stock_reason(self) -> str | None:
        if self.is_bundle:
            return "セット商品・共有在庫のため、在庫は構成品側で管理されています"
        if not self.is_stock_managed:
            return "在庫管理の対象外に設定されているため、在庫数は記録されていません"
        return None


async def sku_profile(session: AsyncSession, master_sku_id: int) -> SkuProfile | None:
    rows = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.is_bundle,
            MasterSku.is_stock_managed,
            MasterSku.archived_at,
            ProductCategory.name,
        )
        .outerjoin(ProductCategory, ProductCategory.id == MasterSku.category_id)
        .where(MasterSku.id == master_sku_id)
    )
    row = rows.first()
    if row is None:
        return None
    return SkuProfile(*row)


async def sku_series(session: AsyncSession, master_sku_id: int, period: Period) -> list[SkuDay]:
    """One row per day in the window, including days the SKU did nothing.

    Gaps are filled HERE rather than left to the chart. A stock line that skips
    the days with no movement would compress time — three quiet weeks would
    render as one step — and the reader would misjudge how long a level held.

    `on_hand_qty` stays None on a day with no stock row, which is different from
    zero: it means the SKU was outside the analysable population that day, or
    the rollup has not reached it.
    """
    stock_rows = await session.execute(
        select(SkuDailyStock.stat_date, SkuDailyStock.on_hand_qty, SkuDailyStock.consumed_qty)
        .where(
            SkuDailyStock.master_sku_id == master_sku_id,
            *_within(SkuDailyStock.stat_date, period),
        )
        .order_by(SkuDailyStock.stat_date)
    )
    stock = {d: (q, c) for d, q, c in stock_rows.all()}

    # Summed across channels: this screen answers "how did this SKU do", and a
    # per-channel split belongs on the sales screen where it can be filtered.
    sales_rows = await session.execute(
        select(
            SkuDailySales.stat_date,
            func.coalesce(func.sum(SkuDailySales.quantity), 0),
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
        )
        .where(
            SkuDailySales.master_sku_id == master_sku_id,
            *_within(SkuDailySales.stat_date, period),
        )
        .group_by(SkuDailySales.stat_date)
    )
    sales = {d: (int(q), Decimal(v)) for d, q, v in sales_rows.all()}

    out: list[SkuDay] = []
    for day in period.dates():
        on_hand, consumed = stock.get(day, (None, 0))
        quantity, revenue = sales.get(day, (0, Decimal(0)))
        out.append(
            SkuDay(
                stat_date=day,
                on_hand_qty=on_hand,
                consumed_qty=int(consumed or 0),
                quantity=quantity,
                gross_sales_jpy=revenue,
            )
        )
    return out


# ---------------------------------------------------------------------------
# カテゴリ別売上 (P2-010)
# ---------------------------------------------------------------------------

#: The row that holds SKUs with no category. Present even at zero, because its
#: absence is what a reader would take as "everything is categorised" — and
#: during the client's category rollout that is exactly the wrong conclusion.
UNCLASSIFIED_LABEL = "未分類"


@dataclass(frozen=True, slots=True)
class CategorySales:
    """A 大分類 with its 中分類 beneath it.

    Totals ROLL UP: a parent's figure includes its children, because a client
    reading 構成比 expects the top level to sum to 100%. The children are kept
    alongside rather than folded away so the same table answers both questions.
    """

    category_id: int | None
    name: str
    quantity: int
    gross_sales_jpy: Decimal
    children: list[CategorySales] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return self.quantity + sum(c.total_quantity for c in self.children)

    @property
    def total_sales_jpy(self) -> Decimal:
        return self.gross_sales_jpy + sum(
            (c.total_sales_jpy for c in self.children), start=Decimal(0)
        )


async def category_sales(
    session: AsyncSession, period: Period, *, where: SalesFilter | None = None
) -> list[CategorySales]:
    """Sales grouped by category, rolled up to 大分類, 未分類 row included.

    The unclassified row is appended unconditionally — including when it is
    zero. A category breakdown whose parts quietly sum to less than the headline
    total is the defect this whole module keeps guarding against, and during a
    category rollout the uncategorised share is the number the client most needs
    to watch shrink.
    """
    where = where or SalesFilter()
    parent = aliased(ProductCategory)
    rows = await session.execute(
        select(
            ProductCategory.id,
            ProductCategory.name,
            ProductCategory.parent_id,
            parent.name,
            func.coalesce(func.sum(SkuDailySales.quantity), 0),
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
        )
        .join(ProductCategory, ProductCategory.id == SkuDailySales.category_id)
        .outerjoin(parent, parent.id == ProductCategory.parent_id)
        .where(*_within(SkuDailySales.stat_date, period), *where.conditions())
        .group_by(ProductCategory.id, ProductCategory.name, ProductCategory.parent_id, parent.name)
        .order_by(func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0).desc())
    )

    # Two passes. One pass cannot work: rows come back by revenue, so a child
    # may arrive before its parent, and merging into a placeholder created for
    # the parent would discard whatever the parent itself sold.
    nodes: dict[int, CategorySales] = {}
    parent_of: dict[int, int | None] = {}
    parent_names: dict[int, str] = {}
    for cid, name, parent_id, parent_name, quantity, sales in rows.all():
        nodes[cid] = CategorySales(cid, name, int(quantity), Decimal(sales))
        parent_of[cid] = parent_id
        if parent_id is not None and parent_name:
            parent_names[parent_id] = parent_name

    # A 大分類 that sold nothing directly produced no row of its own, but its
    # children's revenue still belongs under it at the top level.
    for parent_id, parent_name in parent_names.items():
        nodes.setdefault(parent_id, CategorySales(parent_id, parent_name, 0, Decimal(0)))
        parent_of.setdefault(parent_id, None)

    roots: list[CategorySales] = []
    for cid, node in nodes.items():
        holder = parent_of.get(cid)
        if holder is None or holder not in nodes:
            roots.append(node)
        else:
            nodes[holder].children.append(node)

    unclassified = await session.execute(
        select(
            func.coalesce(func.sum(SkuDailySales.quantity), 0),
            func.coalesce(func.sum(SkuDailySales.gross_sales_jpy), 0),
        ).where(
            SkuDailySales.category_id.is_(None),
            *_within(SkuDailySales.stat_date, period),
            *where.conditions_except_category(),
        )
    )
    quantity, sales = unclassified.one()

    out = sorted(roots, key=lambda n: n.total_sales_jpy, reverse=True)
    out.append(CategorySales(None, UNCLASSIFIED_LABEL, int(quantity), Decimal(sales)))
    return out


__all__ = [
    "STALE_AFTER_HOURS",
    "UNCLASSIFIED_LABEL",
    "UNMAPPED_LABEL",
    "UNMAPPED_NOTICE_RATIO",
    "BucketRow",
    "CategorySales",
    "Delta",
    "Kpis",
    "Provenance",
    "SalesFilter",
    "SkuDay",
    "SkuProfile",
    "SkuRow",
    "bucketed_sales",
    "category_sales",
    "channel_share",
    "channels_in_period",
    "daily_sales",
    "period_kpis",
    "provenance",
    "sku_profile",
    "sku_series",
    "top_skus",
]

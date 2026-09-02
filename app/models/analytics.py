"""Daily analytics rollups — the tables every Phase 2 screen reads.

Measured on this dataset: the fleet-wide 在庫推移 replayed from raw
`inventory_events` takes ~1992ms and spills ~13MB to temp; from these rollups
172ms; from the 1-row-per-day KPI table 2.4ms. Production is db-f1-micro with a
shared vCPU that also runs Rakuten polling every five minutes, so a dashboard
seq-scan does not merely render slowly — it starves order ingestion, which is an
inventory-correctness failure rather than a performance complaint.

Two shapes on purpose:

`sku_daily_stock` is a **dense** grid — one row per (day, SKU) whether or not
anything moved. A stock level is a state, not an event: "what did we hold on the
14th" has an answer for every SKU on every day, and a sparse table would make
every chart carry gap-filling logic.

`sku_daily_sales` is **sparse** — a row only where something sold. Most SKUs
sell on most days not at all, and a dense sales grid would be ~650 x 365 rows of
mostly zeroes.

`stat_date` is a JST calendar date everywhere, produced by
`app.services.timeframe`. Never a UTC date: see that module for why.

No index DDL is declared here beyond the primary keys and the uniqueness the
rollup depends on. Per docs/24, migration 0009 owns every hot-path index; two
migrations creating the same index name is how `alembic upgrade head` fails in
production while CI stays green.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SkuDailyStock(Base):
    """One row per (JST day, SKU): what we held and what it was worth.

    Dense. Built from the SKU population as it stood AT BUILD TIME, so the
    exclusions (bundle parents, 在庫管理対象外, archived) are baked into history
    rather than re-evaluated later — otherwise flagging one gift box today would
    silently rewrite last quarter's stock chart.
    """

    __tablename__ = "sku_daily_stock"
    __table_args__ = (
        UniqueConstraint("stat_date", "master_sku_id", name="uq_sku_daily_stock_day_sku"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    master_sku_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("master_skus.id", ondelete="CASCADE"), nullable=False
    )
    #: Closing on-hand at 24:00 JST that day.
    on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Units consumed by orders that day (positive number).
    consumed_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Units returned by cancellations that day (positive number).
    returned_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # --- reserved for cost, which the client has not supplied ---------------
    # Nullable and unused until 原価 arrives (P2-014). Declared now so enabling
    # stock valuation is a rebuild rather than a migration on a table that by
    # then holds a year of rows — an ALTER on a large table is the expensive
    # kind of change, and this costs nothing while empty.
    unit_cost_jpy: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    stock_value_jpy: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)


class SkuDailySales(Base):
    """One row per (JST day, channel, SKU) THAT SOLD. Sparse by design.

    Attributed to the ordered SKU — the bundle parent, not its components. A set
    sale is one sale of the set; counting the components would double-count
    revenue. Stock movement is the mirror image and lives in `sku_daily_stock`,
    where the components are what actually moved.
    """

    __tablename__ = "sku_daily_sales"
    __table_args__ = (
        UniqueConstraint(
            "stat_date", "channel", "master_sku_id", name="uq_sku_daily_sales_day_channel_sku"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    master_sku_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("master_skus.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised so a category rollup does not join master_skus, and so the
    #: category a sale was MADE under survives a later re-categorisation.
    category_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_categories.id", ondelete="SET NULL"), nullable=True
    )
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gross_sales_jpy: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    #: Cancellations are subtracted rather than deleted, so a day can legitimately
    #: go negative — that is a refund, not a bug, and hiding it would break the
    #: reconciliation against the shop's own figures.
    cancelled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_sales_jpy: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )


class DailyUnmappedSales(Base):
    """Sales that never resolved to a master SKU, kept rather than dropped.

    Every other table here is keyed by master_sku_id, so an order line with
    master_sku_id IS NULL has nowhere to go — and silently discarding it makes
    the analytics disagree with the shop's real revenue by an amount nobody can
    see. Production currently has 156 such alerts outstanding. This table is how
    that gap stays visible and quantified.
    """

    __tablename__ = "daily_unmapped_sales"
    __table_args__ = (
        UniqueConstraint(
            "stat_date", "channel", "channel_sku", name="uq_daily_unmapped_day_channel_sku"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gross_sales_jpy: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )


class DailyKpiSnapshot(Base):
    """One row per JST day — the whole-business figures.

    Exists because the fleet-level chart is the most-loaded query on the
    dashboard and reading it from `sku_daily_stock` still means aggregating ~650
    rows per day. At one row per day it is 2.4ms, which is what makes the
    dashboard openable on a shared vCPU.
    """

    __tablename__ = "daily_kpi_snapshots"
    __table_args__ = (UniqueConstraint("stat_date", name="uq_daily_kpi_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)

    total_on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SKUs in the analysable population that day, so a later archive does not
    #: retroactively change what "all SKUs" meant.
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_of_stock_sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negative_stock_sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sold_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gross_sales_jpy: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    #: Carried alongside the totals rather than in a separate table, so any
    #: screen showing revenue can show what it is missing in the same breath.
    unmapped_sales_jpy: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    #: Only measured for today's build (comparing to live snapshots); NULL for
    #: historical days, where there is nothing current to compare against.
    snapshot_drift_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_value_jpy: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)


class AnalyticsRollupRun(Base, TimestampMixin):
    """Execution log for the rollup job.

    Deliberately NO unique constraint on (job_name, stat_date): the hourly job
    rebuilds recent days repeatedly by design, and a uniqueness error there
    would turn normal operation into a failure. Uniqueness belongs on the DATA
    tables, which it is on.

    Written in a session of its own and committed independently, so a rollback
    of the data transaction does not erase the record that the attempt happened
    — the failure log is the thing you most need when the data did not land.
    """

    __tablename__ = "analytics_rollup_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Range actually rebuilt this run, derived from changed source rows rather
    #: than a fixed trailing window — see AnalyticsRollupService.
    first_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_rebuilt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "AnalyticsRollupRun",
    "DailyKpiSnapshot",
    "DailyUnmappedSales",
    "SkuDailySales",
    "SkuDailyStock",
]

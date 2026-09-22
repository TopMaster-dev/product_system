"""販売速度・欠品予測・動的低在庫閾値 (P2-015 / P2-016 / P2-017).

Velocity is units consumed per day. Three decisions in here matter more than
the arithmetic, because the arithmetic is a division.

**Velocity comes from inventory EVENTS, not from sales lines.**
`sku_daily_stock.consumed_qty` aggregates `order_consumed`, which the ingest
path has already fanned out to stock-holding components. `sku_daily_sales`
records the master that was SOLD, which for a set or a shared-stock parent is
not the master whose stock moved. Using sales would predict a stockout for a
bundle parent that holds nothing, and predict none for the component actually
draining. They are different questions and this is the stock one.

**A number that can be computed is not the same as a number that means
something.** Variant-level history starts 2026-07-20, so early windows hold
days that do not exist, and a SKU with four days of data has a velocity but not
an estimate. `Velocity.confidence` says which it is, and every screen is
expected to show it. Silence would let "残り 2.3 日" stand next to a figure
derived from one afternoon.

**Zero velocity is not zero risk, and it is not a forecast either.** A SKU that
has not sold has no days-remaining; None is the honest answer and the screens
render it as an em dash. Reporting it as infinite days would sort it to the
safe end of a list it does not belong in at all.

There is a fourth caveat this module cannot fix. Many stock baselines are still
inherited from CROSS MALL's ledger rather than counted (573 差分 as of
2026-09-17), so a days-remaining figure is arithmetically correct and factually
meaningless until the October stocktake. `days_remaining` is honest about the
division; only the operator knows whether the numerator is real, so the screens
carry the warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import MasterSku, SkuDailyStock, SkuVelocity
from app.services.sku_scope import analysable_conditions
from app.services.timeframe import Period

log = get_logger(__name__)

#: The window velocity is measured over. Four whole weeks so weekday effects
#: cancel — a 30-day window contains an uneven number of weekends, and this
#: catalogue sells materially more at weekends.
DEFAULT_WINDOW_DAYS = 28

#: Below this many days of recorded history, a velocity is arithmetic rather
#: than an estimate. One week is the shortest span that contains every weekday
#: exactly once.
MIN_DAYS_FOR_ESTIMATE = 7

#: Days of cover the dynamic threshold aims to hold (P2-017). A SKU selling 3/day
#: is "low" below 42; one selling 0.1/day is low below 1.4 -> floored to
#: MIN_THRESHOLD. Fourteen days is the client's stated reorder-to-shelf time.
DEFAULT_COVER_DAYS = 14

#: Even a SKU that barely sells should warn before it hits zero, and a threshold
#: that rounds to 0 would mean "warn me once it is already out".
MIN_THRESHOLD = 3

#: Above this, the threshold stops being a warning and becomes every row on the
#: screen. A SKU selling 50/day would otherwise be "low" below 700.
MAX_THRESHOLD = 200


class Confidence(StrEnum):
    """How much the history behind a velocity supports acting on it."""

    #: A full window of recorded days.
    GOOD = "good"
    #: Enough to estimate, but the window is partly before the data starts.
    LIMITED = "limited"
    #: Too little history. The number exists; the estimate does not.
    INSUFFICIENT = "insufficient"


CONFIDENCE_LABELS: dict[Confidence, str] = {
    Confidence.GOOD: "十分",
    Confidence.LIMITED: "データ少",
    Confidence.INSUFFICIENT: "判定不可",
}


@dataclass(frozen=True, slots=True)
class Velocity:
    """One SKU's consumption rate, with the history behind it."""

    master_sku_id: int
    #: Units consumed across the whole window.
    consumed_qty: int
    #: Days that produced a stock row. The denominator — NOT the window length,
    #: which would read low for any SKU younger than the window.
    days_observed: int
    window_days: int

    @property
    def per_day(self) -> float:
        if self.days_observed <= 0:
            return 0.0
        return self.consumed_qty / self.days_observed

    @property
    def confidence(self) -> Confidence:
        if self.days_observed < MIN_DAYS_FOR_ESTIMATE:
            return Confidence.INSUFFICIENT
        if self.days_observed < self.window_days:
            return Confidence.LIMITED
        return Confidence.GOOD

    @property
    def is_actionable(self) -> bool:
        """Enough history AND actual movement. Both are required: a well-observed
        SKU that sold nothing supports no forecast either."""
        return self.confidence is not Confidence.INSUFFICIENT and self.per_day > 0

    def days_remaining(self, on_hand_qty: int) -> float | None:
        """Days until the stock reaches zero at this rate.

        None rather than a number whenever the answer would be invented:

        * no movement — there is no rate to divide by, and infinity would sort
          to the safe end of a list it does not belong in;
        * too little history — the division would succeed and mean nothing;
        * already at or below zero — it did not run out in N days' time, it has
          run out, and the stock screen says so in its own language.
        """
        if not self.is_actionable or on_hand_qty <= 0:
            return None
        return on_hand_qty / self.per_day

    def stockout_on(self, on_hand_qty: int, *, today: date) -> date | None:
        days = self.days_remaining(on_hand_qty)
        if days is None:
            return None
        # Floor, not round: predicting the day it runs out one day late is the
        # direction that costs a sale.
        return today + timedelta(days=int(days))

    def threshold(self, *, cover_days: int = DEFAULT_COVER_DAYS) -> int:
        """The dynamic low-stock threshold (P2-017).

        Enough stock to cover `cover_days` at this rate, clamped. A SKU with no
        usable history keeps the floor rather than dropping to zero — an unknown
        rate is not a safe one.
        """
        if not self.is_actionable:
            return MIN_THRESHOLD
        raw = self.per_day * cover_days
        return max(MIN_THRESHOLD, min(MAX_THRESHOLD, round(raw)))


async def velocities(
    session: AsyncSession,
    period: Period,
    *,
    master_sku_ids: list[int] | None = None,
) -> dict[int, Velocity]:
    """Consumption rate per SKU over the window.

    Reads `consumed_qty`, which counts `order_consumed` events only —
    cancellations are carried separately in `returned_qty` and are deliberately
    NOT netted off. A cancelled order did not represent demand, but it also did
    not consume stock, and the two corrections would cancel out only if every
    cancellation fell inside the same window as its order. Netting across the
    boundary would produce negative velocity.
    """
    stmt = (
        select(
            SkuDailyStock.master_sku_id,
            func.coalesce(func.sum(SkuDailyStock.consumed_qty), 0),
            func.count(),
        )
        .where(
            SkuDailyStock.stat_date >= period.first_day,
            SkuDailyStock.stat_date <= period.last_day,
        )
        .group_by(SkuDailyStock.master_sku_id)
    )
    if master_sku_ids is not None:
        if not master_sku_ids:
            return {}
        stmt = stmt.where(SkuDailyStock.master_sku_id.in_(master_sku_ids))

    rows = await session.execute(stmt)
    return {
        master_id: Velocity(
            master_sku_id=master_id,
            consumed_qty=int(consumed),
            days_observed=int(days),
            window_days=period.days,
        )
        for master_id, consumed, days in rows.all()
    }


@dataclass(frozen=True, slots=True)
class StockoutRisk:
    """A SKU ranked by how soon it runs out (P2-018)."""

    master_sku_id: int
    sku_code: str
    name: str
    on_hand_qty: int
    velocity: Velocity
    threshold: int
    days_remaining: float | None
    stockout_on: date | None

    @property
    def is_below_threshold(self) -> bool:
        return self.on_hand_qty < self.threshold


async def stockout_risks(
    session: AsyncSession,
    period: Period,
    *,
    today: date,
    cover_days: int = DEFAULT_COVER_DAYS,
    limit: int | None = None,
) -> list[StockoutRisk]:
    """SKUs ordered by days remaining, soonest first.

    SKUs with no forecast are NOT dropped — they are placed after the forecast
    ones. A SKU sitting at zero has no days-remaining precisely because it has
    already run out, and removing it would take the most urgent rows off a
    screen whose whole job is triage.
    """
    snapshot = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            func.coalesce(SkuDailyStock.on_hand_qty, 0),
        )
        .join(
            SkuDailyStock,
            (SkuDailyStock.master_sku_id == MasterSku.id)
            & (SkuDailyStock.stat_date == period.last_day),
            isouter=True,
        )
        .where(*analysable_conditions(include_archived=False))
    )
    rows = snapshot.all()
    rates = await velocities(session, period, master_sku_ids=[r[0] for r in rows])

    risks: list[StockoutRisk] = []
    for master_id, code, name, on_hand in rows:
        rate = rates.get(master_id) or Velocity(master_id, 0, 0, period.days)
        qty = int(on_hand or 0)
        risks.append(
            StockoutRisk(
                master_sku_id=master_id,
                sku_code=code,
                name=name,
                on_hand_qty=qty,
                velocity=rate,
                threshold=rate.threshold(cover_days=cover_days),
                days_remaining=rate.days_remaining(qty),
                stockout_on=rate.stockout_on(qty, today=today),
            )
        )

    # Already out first, then soonest-to-run-out, then everything else. Sorting
    # None to the end with a sentinel keeps the unforecastable rows visible
    # rather than dropping them.
    risks.sort(
        key=lambda r: (
            0 if r.on_hand_qty <= 0 else 1,
            r.days_remaining if r.days_remaining is not None else float("inf"),
            -r.velocity.per_day,
        )
    )
    return risks[:limit] if limit else risks


async def refresh_velocities(
    session: AsyncSession,
    period: Period,
    *,
    now: datetime,
    cover_days: int = DEFAULT_COVER_DAYS,
) -> int:
    """Recompute `sku_velocity` for every analysable SKU. Returns rows written.

    Rewrites the whole table rather than upserting the SKUs that moved. A SKU
    that stopped selling must have its velocity fall — an upsert keyed on
    movement would leave its last busy figure in place forever, and the
    threshold derived from it would keep flagging a dormant line as urgent.

    Every analysable SKU gets a row, including ones with no stock history at
    all. Absence would be read by the join as "no threshold", and the inventory
    screen would fall back to the fixed default for exactly the SKUs it knows
    least about.

    The caller owns the transaction.
    """
    population = await session.execute(
        select(MasterSku.id).where(*analysable_conditions(include_archived=False))
    )
    ids = [i for (i,) in population.all()]
    if not ids:
        await session.execute(delete(SkuVelocity))
        return 0

    rates = await velocities(session, period, master_sku_ids=ids)

    # DELETE + INSERT, matching how the daily rollup rebuilds a day: it has to
    # be able to REMOVE rows for SKUs that left the population, which an upsert
    # cannot do.
    await session.execute(delete(SkuVelocity))
    rows = []
    for master_id in ids:
        rate = rates.get(master_id) or Velocity(master_id, 0, 0, period.days)
        rows.append(
            {
                "master_sku_id": master_id,
                "window_days": period.days,
                "days_observed": rate.days_observed,
                "consumed_qty": rate.consumed_qty,
                "per_day": Decimal(f"{rate.per_day:.4f}"),
                "low_stock_threshold": rate.threshold(cover_days=cover_days),
                "confidence": rate.confidence.value,
                "computed_at": now,
            }
        )
    await session.execute(insert(SkuVelocity), rows)
    log.info("velocity.refreshed", skus=len(rows), window_days=period.days)
    return len(rows)


__all__ = [
    "CONFIDENCE_LABELS",
    "DEFAULT_COVER_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_THRESHOLD",
    "MIN_DAYS_FOR_ESTIMATE",
    "MIN_THRESHOLD",
    "Confidence",
    "StockoutRisk",
    "Velocity",
    "refresh_velocities",
    "stockout_risks",
    "velocities",
]

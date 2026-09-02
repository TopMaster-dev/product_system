"""JST day boundaries — the single implementation every aggregate shares.

Every timestamp in this system is stored UTC. Every report the client reads is
JST. That gap is nine hours wide, and a naive `::date` rollup misfiles every
order placed between 00:00 and 09:00 JST — roughly a third of a day's volume —
into the previous day. The numbers would still look plausible, which is the
dangerous part: nothing errors, the daily totals are simply wrong, and the
hand-reconciliation in P2-042 fails with no obvious cause.

So one module owns the conversion, and everything else calls into it.

TWO RULES THAT ARE EASY TO GET WRONG
------------------------------------

**1. Build `jst_date_expr()` ONCE and reuse the object.**

    stat_date = jst_date_expr(Order.ordered_at)      # once
    select(stat_date, func.count()).group_by(stat_date)   # same object twice

Calling it twice produces two equal-but-distinct SQLAlchemy expressions with
separate bind parameters. Postgres then cannot match the GROUP BY term to the
SELECT term and raises:

    GroupingError: column "orders.ordered_at" must appear in the GROUP BY
    clause or be used in an aggregate function

It looks like a query-authoring mistake rather than a timezone one, which is
why it costs an afternoon the first time.

**2. Never wrap an indexed column in a WHERE clause.**

    WHERE timezone('Asia/Tokyo', occurred_at)::date = '2026-08-01'   -- seq scan
    WHERE occurred_at >= :start AND occurred_at < :end                -- index scan

`jst_day_bounds()` / `jst_range_bounds()` precompute the UTC half-open interval
so the index still applies. On db-f1-micro — shared vCPU, also running Rakuten
polling every five minutes — a seq scan over `inventory_events` does not merely
render a slow page, it starves order ingestion.

The `tzdata` package is a hard dependency, not a convenience: python:*-slim
carries no system timezone database, so ZoneInfo would raise at import inside
the container. The failure is at startup rather than at query time, which is at
least loud — but it would be loud in production.

JST is UTC+9 with no daylight saving, so a JST day is always exactly the UTC
interval [15:00 previous day, 15:00). The conversions here go through ZoneInfo
regardless, rather than hard-coding +9, so nothing has to be revisited if this
ever runs against another market.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Date, cast, func

JST_NAME = "Asia/Tokyo"
JST = ZoneInfo(JST_NAME)

#: Preset windows offered by the analytics screens, in trailing whole JST days
#: ending yesterday. "Ending yesterday" and not "today" because today is
#: incomplete: a partial day dropped into a trend line reads as a collapse in
#: sales every morning.
PRESET_DAYS: dict[str, int] = {
    "7d": 7,
    "28d": 28,
    "90d": 90,
    "180d": 180,
    "365d": 365,
}

#: Used when a caller supplies something unrecognised. Four whole weeks, so
#: weekday effects cancel rather than skewing whichever days happen to be
#: included — a 30-day window contains an uneven number of weekends.
DEFAULT_PRESET = "28d"

#: A year ago, as 52 whole weeks rather than a calendar year. Retail comparisons
#: are weekday-sensitive (weekends outsell Tuesdays), and 365 days shifts the
#: weekday by one, so a Saturday gets compared against a Friday. 364 keeps the
#: alignment; the cost is that the date drifts by a day or two, which matters
#: far less than the weekday.
PREVIOUS_YEAR_SHIFT = timedelta(days=364)


def jst_date_expr(column: ColumnElement[datetime]) -> ColumnElement[date]:
    """SQL for "which JST calendar day is this UTC timestamp in".

    Assign the result to a variable and reuse THAT OBJECT in both SELECT and
    GROUP BY — see rule 1 in the module docstring. Never put this in a WHERE
    clause; use `jst_day_bounds` / `jst_range_bounds` instead.
    """
    return cast(func.timezone(JST_NAME, column), Date)


def jst_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC half-open interval [start, end) covering one JST calendar day.

    Half-open, so consecutive days neither overlap nor leave a gap — an order at
    exactly 00:00:00 JST belongs to the day starting then, and to nothing else.
    """
    start = datetime.combine(day, time.min, tzinfo=JST).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=JST).astimezone(UTC)
    return start, end


def jst_range_bounds(first_day: date, last_day: date) -> tuple[datetime, datetime]:
    """UTC bounds covering [first_day, last_day] inclusive of both JST days."""
    start, _ = jst_day_bounds(first_day)
    _, end = jst_day_bounds(last_day)
    return start, end


def to_jst_date(moment: datetime) -> date:
    """The JST calendar day a UTC instant falls in — the Python counterpart of
    `jst_date_expr`, kept in lockstep with it by tests."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(JST).date()


def today_jst(now: datetime) -> date:
    """`now` is passed in rather than read from the clock so every caller is
    testable and the rollup can be re-run for a historical instant."""
    return to_jst_date(now)


@dataclass(frozen=True, slots=True)
class Period:
    """A closed range of whole JST days, plus how to say it in a URL."""

    first_day: date
    last_day: date
    preset: str

    @property
    def days(self) -> int:
        return (self.last_day - self.first_day).days + 1

    def utc_bounds(self) -> tuple[datetime, datetime]:
        return jst_range_bounds(self.first_day, self.last_day)

    def previous(self) -> Period:
        """The equally long window immediately before this one, with no gap and
        no overlap — the honest comparison for "vs 前期間"."""
        last = self.first_day - timedelta(days=1)
        first = last - timedelta(days=self.days - 1)
        return Period(first, last, self.preset)

    def previous_year(self) -> Period:
        """Shifted 364 days so weekdays line up. See PREVIOUS_YEAR_SHIFT."""
        return Period(
            self.first_day - PREVIOUS_YEAR_SHIFT,
            self.last_day - PREVIOUS_YEAR_SHIFT,
            self.preset,
        )

    def dates(self) -> list[date]:
        return [self.first_day + timedelta(days=i) for i in range(self.days)]


def resolve_period(preset: str | None, *, now: datetime) -> Period:
    """Turn a query-string preset into whole JST days ending YESTERDAY.

    An unrecognised value falls back to the default rather than raising. A
    dashboard is a read-only view someone reaches by editing a URL or following
    a stale bookmark; a 500 there is a worse answer than 28 days of data.
    """
    days = PRESET_DAYS.get(preset or "", PRESET_DAYS[DEFAULT_PRESET])
    key = preset if preset in PRESET_DAYS else DEFAULT_PRESET
    last_day = today_jst(now) - timedelta(days=1)
    first_day = last_day - timedelta(days=days - 1)
    return Period(first_day, last_day, key)


def pct_change(current: float | int | None, baseline: float | int | None) -> float | None:
    """Percentage change, or None when it cannot be expressed.

    None — not 0, not infinity — when the baseline is zero or missing. Growth
    from nothing has no percentage, and rendering it as +100% or 0% invents a
    figure the client would reasonably act on.
    """
    if current is None or baseline is None or baseline == 0:
        return None
    return (current - baseline) / baseline * 100.0


def week_start(day: date) -> date:
    """Monday of the week containing `day`.

    Monday-based because Japanese retail weeks and the client's own reporting
    run Monday to Sunday; Python's `weekday()` already treats Monday as 0.
    """
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)


def bucket(day: date, granularity: str) -> date:
    """Collapse a day into its week or month bucket.

    Unknown granularity returns the day unchanged rather than raising, matching
    `resolve_period`: these values arrive from query strings.
    """
    if granularity == "week":
        return week_start(day)
    if granularity == "month":
        return month_start(day)
    return day


__all__ = [
    "DEFAULT_PRESET",
    "JST",
    "JST_NAME",
    "PRESET_DAYS",
    "PREVIOUS_YEAR_SHIFT",
    "Period",
    "bucket",
    "jst_date_expr",
    "jst_day_bounds",
    "jst_range_bounds",
    "month_start",
    "pct_change",
    "resolve_period",
    "to_jst_date",
    "today_jst",
    "week_start",
]

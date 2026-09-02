"""Unit tests for JST day boundaries.

The load-bearing case is 02:30 JST. Every timestamp is stored UTC, so an order
placed at 02:30 JST is 17:30 UTC on the *previous* day — and a naive `::date`
rollup files it under the wrong day. Roughly a third of a day's orders fall in
that 00:00-09:00 JST window, so the error is large enough to matter and quiet
enough to survive review.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.timeframe import (
    DEFAULT_PRESET,
    JST,
    PRESET_DAYS,
    Period,
    bucket,
    jst_date_expr,
    jst_day_bounds,
    jst_range_bounds,
    month_start,
    pct_change,
    resolve_period,
    to_jst_date,
    week_start,
)

pytestmark = pytest.mark.unit


# --- the 02:30 JST case --------------------------------------------------


def test_an_order_at_0230_jst_belongs_to_that_jst_day() -> None:
    """02:30 JST on 2026-08-02 is 17:30 UTC on 2026-08-01. It must roll up
    under 08-02, the day the shop actually made the sale."""
    moment = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)
    assert to_jst_date(moment) == date(2026, 8, 2)


def test_the_utc_bounds_of_a_jst_day_span_the_previous_afternoon() -> None:
    start, end = jst_day_bounds(date(2026, 8, 2))
    assert start == datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 2, 15, 0, tzinfo=UTC)


def test_bounds_are_half_open_so_days_neither_gap_nor_overlap() -> None:
    """An order at exactly 00:00:00 JST belongs to the day starting then, and
    to nothing else."""
    _, end_of_first = jst_day_bounds(date(2026, 8, 1))
    start_of_second, _ = jst_day_bounds(date(2026, 8, 2))
    assert end_of_first == start_of_second

    midnight_jst = datetime(2026, 8, 2, 0, 0, tzinfo=JST).astimezone(UTC)
    assert midnight_jst == start_of_second
    assert to_jst_date(midnight_jst) == date(2026, 8, 2)


def test_the_last_instant_of_a_jst_day_stays_in_that_day() -> None:
    last = datetime(2026, 8, 2, 23, 59, 59, tzinfo=JST).astimezone(UTC)
    start, end = jst_day_bounds(date(2026, 8, 2))
    assert start <= last < end
    assert to_jst_date(last) == date(2026, 8, 2)


def test_naive_utc_dates_would_disagree_for_the_whole_morning() -> None:
    """Quantifies what the module exists to prevent: every JST hour from 00:00
    to 08:59 has a different UTC calendar date."""
    disagreements = 0
    for hour in range(24):
        moment = datetime(2026, 8, 2, hour, 0, tzinfo=JST).astimezone(UTC)
        if moment.date() != to_jst_date(moment):
            disagreements += 1
    assert disagreements == 9  # 00:00-08:59 JST


def test_range_bounds_cover_both_endpoint_days_inclusively() -> None:
    start, end = jst_range_bounds(date(2026, 8, 1), date(2026, 8, 3))
    assert start == jst_day_bounds(date(2026, 8, 1))[0]
    assert end == jst_day_bounds(date(2026, 8, 3))[1]


def test_naive_datetimes_are_treated_as_utc() -> None:
    """Defensive: a naive value here means someone lost a tzinfo upstream, and
    guessing local time would be silently wrong on a JST developer machine."""
    assert to_jst_date(datetime(2026, 8, 1, 17, 30)) == date(2026, 8, 2)


# --- the SQL expression --------------------------------------------------


def test_expression_converts_before_truncating() -> None:
    from app.models import Order

    sql = str(jst_date_expr(Order.ordered_at).compile(compile_kwargs={"literal_binds": True}))
    assert "Asia/Tokyo" in sql
    assert "CAST" in sql.upper()


def test_two_calls_produce_distinct_objects() -> None:
    """Why the docstring insists on building it once: separate objects carry
    separate bind params, and Postgres then cannot match GROUP BY to SELECT."""
    from app.models import Order

    assert jst_date_expr(Order.ordered_at) is not jst_date_expr(Order.ordered_at)


# --- period resolution ---------------------------------------------------


def test_presets_end_yesterday_not_today() -> None:
    """Today is incomplete; including it makes every morning look like a crash
    in sales."""
    now = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)  # 12:00 JST on the 10th
    period = resolve_period("7d", now=now)
    assert period.last_day == date(2026, 8, 9)
    assert period.first_day == date(2026, 8, 3)
    assert period.days == 7


@pytest.mark.parametrize("preset", sorted(PRESET_DAYS))
def test_every_preset_resolves_to_its_advertised_length(preset: str) -> None:
    period = resolve_period(preset, now=datetime(2026, 8, 10, 3, 0, tzinfo=UTC))
    assert period.days == PRESET_DAYS[preset]


@pytest.mark.parametrize("bad", ["", None, "42d", "; DROP TABLE orders", "７日"])
def test_unrecognised_presets_fall_back_instead_of_raising(bad: str | None) -> None:
    """A dashboard is reached by editing a URL or following a stale bookmark; a
    500 there is a worse answer than the default window."""
    period = resolve_period(bad, now=datetime(2026, 8, 10, 3, 0, tzinfo=UTC))
    assert period.days == PRESET_DAYS[DEFAULT_PRESET]
    assert period.preset == DEFAULT_PRESET


def test_previous_period_abuts_without_gap_or_overlap() -> None:
    period = resolve_period("7d", now=datetime(2026, 8, 10, 3, 0, tzinfo=UTC))
    previous = period.previous()
    assert previous.days == period.days
    assert previous.last_day == period.first_day - timedelta(days=1)


def test_previous_year_preserves_the_weekday() -> None:
    """364 days, not 365: retail is weekday-sensitive, and a calendar year
    shifts Saturday onto Friday."""
    period = resolve_period("28d", now=datetime(2026, 8, 10, 3, 0, tzinfo=UTC))
    last_year = period.previous_year()
    assert last_year.first_day.weekday() == period.first_day.weekday()
    assert last_year.last_day.weekday() == period.last_day.weekday()


def test_period_enumerates_every_day_once() -> None:
    period = Period(date(2026, 8, 1), date(2026, 8, 5), "5d")
    days = period.dates()
    assert len(days) == 5 == period.days
    assert len(set(days)) == 5
    assert days[0] == period.first_day and days[-1] == period.last_day


def test_a_single_day_period_is_one_day_long() -> None:
    """Off-by-one guard: inclusive bounds mean first == last is 1, not 0."""
    assert Period(date(2026, 8, 1), date(2026, 8, 1), "1d").days == 1


# --- pct_change ----------------------------------------------------------


def test_pct_change_basic() -> None:
    assert pct_change(150, 100) == pytest.approx(50.0)
    assert pct_change(50, 100) == pytest.approx(-50.0)
    assert pct_change(100, 100) == pytest.approx(0.0)


@pytest.mark.parametrize(("current", "baseline"), [(10, 0), (0, 0), (None, 5), (5, None)])
def test_pct_change_is_none_when_it_cannot_be_expressed(current, baseline) -> None:
    """Not 0 and not infinity. Growth from nothing has no percentage, and
    inventing one gives the client a figure they would act on."""
    assert pct_change(current, baseline) is None


# --- buckets -------------------------------------------------------------


def test_week_starts_on_monday() -> None:
    # 2026-08-05 is a Wednesday.
    assert week_start(date(2026, 8, 5)) == date(2026, 8, 3)
    assert week_start(date(2026, 8, 3)) == date(2026, 8, 3)  # Monday is its own start


def test_month_bucket() -> None:
    assert month_start(date(2026, 8, 31)) == date(2026, 8, 1)


def test_unknown_granularity_passes_the_day_through() -> None:
    day = date(2026, 8, 5)
    assert bucket(day, "week") == date(2026, 8, 3)
    assert bucket(day, "month") == date(2026, 8, 1)
    assert bucket(day, "nonsense") == day

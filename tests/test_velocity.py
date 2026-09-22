"""販売速度・欠品予測・動的閾値 (P2-015..017).

The arithmetic is a division. What this file defends is everything around it:
when the division must NOT be performed, and what the answer is instead.

Three states all render as "no forecast" and mean different things — no
movement, not enough history, already at zero — and every one of them has a
tempting numeric answer that would be read as fact. Infinity sorts a
never-selling SKU to the safe end of a triage list. Dividing four days of data
puts "残り 1.2 日" on screen. A SKU at zero did not run out in some number of
days' time; it has run out.

The acceptance criterion for P2-016 is explicit that velocity-zero SKUs are
excluded from the forecast, and for P2-017 that faster SKUs get higher
thresholds. Both are here.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.timeframe import Period
from app.services.velocity import (
    MAX_THRESHOLD,
    MIN_DAYS_FOR_ESTIMATE,
    MIN_THRESHOLD,
    Confidence,
    Velocity,
    stockout_risks,
    velocities,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 29)
PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")


def _v(consumed: int, days: int, window: int = 28) -> Velocity:
    return Velocity(master_sku_id=1, consumed_qty=consumed, days_observed=days, window_days=window)


# --- the rate itself ------------------------------------------------------


def test_velocity_is_units_per_observed_day() -> None:
    assert _v(56, 28).per_day == 2.0


def test_the_denominator_is_observed_days_not_the_window() -> None:
    """A SKU registered a week ago has 7 days of history inside a 28-day window.
    Dividing by 28 would report a quarter of its real rate, and it would be the
    fast movers — the newest, best-selling lines — that read as dormant."""
    assert _v(14, 7).per_day == 2.0


def test_no_observed_days_is_a_rate_of_zero_not_a_crash() -> None:
    assert _v(0, 0).per_day == 0.0


# --- confidence -----------------------------------------------------------


def test_a_full_window_is_good_confidence() -> None:
    assert _v(56, 28).confidence is Confidence.GOOD


def test_a_partial_window_is_limited() -> None:
    """Real for every SKU younger than the window, and for every window reaching
    back before 2026-07-20 when variant history began."""
    assert _v(20, 10).confidence is Confidence.LIMITED


def test_under_a_week_is_insufficient() -> None:
    assert _v(20, MIN_DAYS_FOR_ESTIMATE - 1).confidence is Confidence.INSUFFICIENT


def test_exactly_a_week_is_enough_to_estimate() -> None:
    assert _v(7, MIN_DAYS_FOR_ESTIMATE).confidence is Confidence.LIMITED


def test_movement_alone_is_not_actionable() -> None:
    """Four days of data produces a rate. It does not produce an estimate."""
    assert _v(40, 4).per_day == 10.0
    assert _v(40, 4).is_actionable is False


def test_history_alone_is_not_actionable_either() -> None:
    """A fully observed SKU that sold nothing supports no forecast."""
    assert _v(0, 28).confidence is Confidence.GOOD
    assert _v(0, 28).is_actionable is False


# --- days remaining -------------------------------------------------------


def test_days_remaining_is_stock_over_rate() -> None:
    assert _v(56, 28).days_remaining(100) == 50.0


def test_a_sku_that_never_sold_has_no_forecast_rather_than_infinite_days() -> None:
    """P2-016's acceptance criterion. Infinity would sort it to the safe end of
    a triage list it does not belong in at all."""
    assert _v(0, 28).days_remaining(100) is None


def test_too_little_history_produces_no_forecast() -> None:
    assert _v(40, 4).days_remaining(100) is None


def test_a_sku_at_zero_has_no_days_remaining() -> None:
    """It did not run out in N days' time. It has run out, and the stock screen
    says that in its own language."""
    assert _v(56, 28).days_remaining(0) is None


def test_negative_stock_has_no_days_remaining_either() -> None:
    assert _v(56, 28).days_remaining(-4) is None


def test_the_stockout_date_floors_rather_than_rounds() -> None:
    """2.9 days becomes "in 2 days". Predicting it a day late is the direction
    that costs a sale."""
    v = _v(28, 28)  # 1.0/day
    assert v.stockout_on(2, today=TODAY) == date(2026, 10, 1)
    assert _v(56, 28).stockout_on(5, today=TODAY) == date(2026, 10, 1)


def test_no_forecast_means_no_stockout_date() -> None:
    assert _v(0, 28).stockout_on(100, today=TODAY) is None


# --- the dynamic threshold ------------------------------------------------


def test_a_faster_sku_gets_a_higher_threshold() -> None:
    """P2-017's acceptance criterion, stated directly."""
    slow = _v(28, 28).threshold()  # 1/day -> 14
    fast = _v(140, 28).threshold()  # 5/day -> 70
    assert slow < fast
    assert (slow, fast) == (14, 70)


def test_a_barely_moving_sku_keeps_the_floor() -> None:
    """0.1/day over 14 days is 1.4, which rounds to 1 — a threshold that means
    "warn me once it is nearly gone"."""
    assert _v(3, 28).threshold() == MIN_THRESHOLD


def test_an_unknown_rate_keeps_the_floor_rather_than_dropping_to_zero() -> None:
    """An unknown rate is not a safe one. Zero would mean the SKU never appears
    as low until it is negative."""
    assert _v(0, 28).threshold() == MIN_THRESHOLD
    assert _v(40, 4).threshold() == MIN_THRESHOLD


def test_a_very_fast_sku_is_clamped() -> None:
    """50/day over 14 days is 700. Without the cap, "low stock" would be every
    row on the screen and the badge would stop meaning anything."""
    assert _v(28 * 50, 28).threshold() == MAX_THRESHOLD


def test_cover_days_is_adjustable() -> None:
    assert _v(28, 28).threshold(cover_days=7) == 7


# --- the query ------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = answers

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


async def test_velocities_carries_the_window_length_onto_each_row() -> None:
    """Confidence depends on observed days RELATIVE to the window, so the row
    cannot judge itself without knowing how long the window was."""
    out = await velocities(_FakeSession([[(1, 56, 28), (2, 7, 7)]]), PERIOD)  # type: ignore[arg-type]
    assert out[1].confidence is Confidence.GOOD
    assert out[2].confidence is Confidence.LIMITED


async def test_asking_for_no_skus_queries_nothing() -> None:
    assert await velocities(_FakeSession([]), PERIOD, master_sku_ids=[]) == {}  # type: ignore[arg-type]


# --- the risk list --------------------------------------------------------


async def test_already_out_sorts_above_soonest_to_run_out() -> None:
    """A screen for triage. Something at zero is more urgent than something with
    two days left, and both are more urgent than a SKU with no forecast."""
    session = _FakeSession(
        [
            [
                (1, "A", "runs out soon", 2),
                (2, "B", "already out", 0),
                (3, "C", "never sells", 500),
            ],
            [(1, 28, 28), (2, 28, 28), (3, 0, 28)],
        ]
    )
    risks = await stockout_risks(session, PERIOD, today=TODAY)  # type: ignore[arg-type]
    assert [r.sku_code for r in risks] == ["B", "A", "C"]


async def test_a_sku_with_no_forecast_is_kept_not_dropped() -> None:
    """Dropping it would take the most urgent rows off a triage screen — a SKU
    has no forecast precisely BECAUSE it has already run out."""
    session = _FakeSession([[(1, "A", "n", 0)], [(1, 0, 28)]])
    risks = await stockout_risks(session, PERIOD, today=TODAY)  # type: ignore[arg-type]
    assert len(risks) == 1
    assert risks[0].days_remaining is None


async def test_a_sku_with_no_stock_row_is_treated_as_zero_on_hand() -> None:
    """The outer join yields NULL for a SKU the rollup has no row for on that
    day. Zero is right here: the list is about what to reorder, and an unknown
    holding is not evidence of a healthy one."""
    session = _FakeSession([[(1, "A", "n", None)], [(1, 28, 28)]])
    risks = await stockout_risks(session, PERIOD, today=TODAY)  # type: ignore[arg-type]
    assert risks[0].on_hand_qty == 0


async def test_a_sku_absent_from_the_velocity_rows_still_appears() -> None:
    """No stock rows at all in the window. It must not vanish from the list."""
    session = _FakeSession([[(9, "Z", "new", 5)], []])
    risks = await stockout_risks(session, PERIOD, today=TODAY)  # type: ignore[arg-type]
    assert [r.sku_code for r in risks] == ["Z"]
    assert risks[0].velocity.confidence is Confidence.INSUFFICIENT
    assert risks[0].threshold == MIN_THRESHOLD


async def test_below_threshold_is_computed_against_the_dynamic_value() -> None:
    session = _FakeSession([[(1, "A", "n", 10)], [(1, 140, 28)]])  # 5/day -> 70
    risks = await stockout_risks(session, PERIOD, today=TODAY)  # type: ignore[arg-type]
    assert risks[0].threshold == 70
    assert risks[0].is_below_threshold is True

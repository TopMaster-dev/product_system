"""Unit tests for the pure part of the rollup — the daily balance walk.

Split out because this is the piece most likely to be subtly wrong: it carries
state across days, and an error compounds forward silently rather than failing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.analytics_rollup import is_today, walk_daily_balances

pytestmark = pytest.mark.unit

D1, D2, D3 = date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)


def test_a_sku_that_does_not_move_keeps_yesterdays_balance() -> None:
    """Stock is a state. Resetting an unmoved SKU to zero would show the whole
    catalogue collapsing on any quiet day."""
    out = walk_daily_balances({1: 10}, {}, [D1, D2, D3], [1])
    assert out[(D1, 1)] == 10
    assert out[(D2, 1)] == 10
    assert out[(D3, 1)] == 10


def test_deltas_accumulate_forward() -> None:
    out = walk_daily_balances(
        {1: 10},
        {(D1, 1): -3, (D2, 1): -2, (D3, 1): +5},
        [D1, D2, D3],
        [1],
    )
    assert out[(D1, 1)] == 7
    assert out[(D2, 1)] == 5
    assert out[(D3, 1)] == 10


def test_the_grid_is_dense_across_every_sku_and_day() -> None:
    """Every SKU gets a row every day, so charts never gap-fill."""
    out = walk_daily_balances({}, {}, [D1, D2], [1, 2, 3])
    assert len(out) == 6
    assert all((d, s) in out for d in (D1, D2) for s in (1, 2, 3))


def test_a_sku_with_no_opening_balance_starts_at_zero() -> None:
    out = walk_daily_balances({}, {(D1, 2): 4}, [D1], [1, 2])
    assert out[(D1, 1)] == 0
    assert out[(D1, 2)] == 4


def test_negative_balances_are_carried_not_clamped() -> None:
    """Physical stock cannot be negative, but the LEDGER can — that is how
    oversell is visible. Clamping here would hide it from every chart."""
    out = walk_daily_balances({1: 1}, {(D1, 1): -3}, [D1, D2], [1])
    assert out[(D1, 1)] == -2
    assert out[(D2, 1)] == -2


def test_deltas_for_skus_outside_the_population_are_ignored() -> None:
    """The population is decided by sku_scope; a delta for a bundle parent or an
    archived SKU must not sneak a row into the grid."""
    out = walk_daily_balances({9: 100}, {(D1, 9): 5}, [D1], [1])
    assert list(out) == [(D1, 1)]


def test_days_are_walked_in_the_order_given() -> None:
    """Out-of-order days would apply deltas to the wrong opening balance."""
    ordered = walk_daily_balances({1: 0}, {(D1, 1): 1, (D2, 1): 10}, [D1, D2], [1])
    assert ordered[(D1, 1)] == 1
    assert ordered[(D2, 1)] == 11


def test_is_today_uses_jst_not_utc() -> None:
    """17:30 UTC on 08-01 is 02:30 JST on 08-02 — 'today' is the JST day."""
    now = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)
    assert is_today(date(2026, 8, 2), now) is True
    assert is_today(date(2026, 8, 1), now) is False

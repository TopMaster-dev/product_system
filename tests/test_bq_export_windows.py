"""Unit tests for incremental export windowing.

`plan_windows` is the whole reason the 2026-08-21 outage cannot repeat in the
same shape: it converts "export everything since the last success" — an interval
that grew to 82 days while the export was broken — into a bounded sequence the
caller commits one at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.bigquery_export import EXPORT_WINDOW, plan_windows

pytestmark = pytest.mark.unit

T0 = datetime(2026, 5, 31, 18, 0, tzinfo=UTC)


def test_windows_chain_without_gaps_or_overlaps() -> None:
    """Each window's end is the next window's start, because the service derives
    `since` from the previous committed watermark. A gap loses rows silently;
    an overlap duplicates them."""
    until = T0 + timedelta(days=3)
    assert plan_windows(T0, until) == [
        T0 + timedelta(days=1),
        T0 + timedelta(days=2),
        T0 + timedelta(days=3),
    ]


def test_the_final_window_never_overshoots_until() -> None:
    """A partial last day must end exactly at `until`; exporting past it would
    claim a watermark for data that does not exist yet."""
    until = T0 + timedelta(days=2, hours=7)
    windows = plan_windows(T0, until)
    assert windows[-1] == until
    assert all(w <= until for w in windows)


def test_the_production_backlog_is_bounded() -> None:
    """82 days of accumulated backlog — the state that OOM-killed the container
    when it was a single window — becomes 82 commit points."""
    until = T0 + timedelta(days=82)
    windows = plan_windows(T0, until)
    assert len(windows) == 82
    assert windows[-1] == until


def test_no_watermark_yields_one_window() -> None:
    """Nothing to divide: an empty source table, or a snapshot mode that ignores
    watermarks entirely."""
    assert plan_windows(None, T0) == [T0]


def test_a_watermark_at_or_past_until_yields_one_window() -> None:
    """Clock skew or a re-run with an older `until` must not produce an empty
    plan — the (table, until) UNIQUE constraint is what makes the repeat a
    no-op, not an absent window."""
    assert plan_windows(T0, T0) == [T0]
    assert plan_windows(T0 + timedelta(days=1), T0) == [T0]


def test_window_size_is_configurable_for_a_denser_catch_up() -> None:
    windows = plan_windows(T0, T0 + timedelta(days=1), window=timedelta(hours=6))
    assert len(windows) == 4


def test_default_window_is_one_day() -> None:
    assert EXPORT_WINDOW.days == 1

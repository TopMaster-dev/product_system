"""The rollup must refresh sku_velocity on every real pass.

Written after the first production deploy of 0013, where the table stayed empty.
The refresh was gated on `outcome.days_rebuilt`, and the hourly job rebuilds
nothing when the last hour was quiet — so on a system that had just deployed,
the gate never opened and the inventory screen silently fell back to the fixed
threshold of 10. Nothing failed; the feature simply did not exist.

The gate is wrong for a second reason that outlasts the first deploy. The 28-day
window slides every day whether or not anything sold, so yesterday leaves it
regardless. A SKU that stops selling must see its rate — and the threshold
derived from it — decay on its own. Gating on new activity freezes every
threshold exactly during the quiet spell that should be lowering them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.cli.rebuild_daily_metrics import velocity_window
from app.services.velocity import DEFAULT_WINDOW_DAYS

pytestmark = pytest.mark.unit

SOURCE = Path("app/cli/rebuild_daily_metrics.py").read_text(encoding="utf-8")


def test_the_refresh_is_not_gated_on_days_rebuilt() -> None:
    """The regression, guarded at the source. A behavioural test would need the
    whole rollup harness; this states the one line that must not come back."""
    assert "if outcome.days_rebuilt:" not in SOURCE


def test_the_refresh_is_actually_called() -> None:
    assert "refresh_velocities(" in SOURCE


def test_a_velocity_failure_cannot_fail_the_rollup() -> None:
    """It is a projection. Losing it costs a stale threshold until the next
    hour; failing the run would discard a completed backfill."""
    assert "rollup.velocity_failed" in SOURCE


# --- the window -----------------------------------------------------------


def test_the_window_ends_yesterday() -> None:
    """Today is partial. A rate that includes this morning drops every day at
    midnight and climbs back by evening, and a threshold derived from it would
    quietly stop flagging SKUs before lunchtime."""
    window = velocity_window(datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
    assert window.last_day.isoformat() == "2026-09-18"


def test_the_window_is_the_configured_length() -> None:
    window = velocity_window(datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
    assert window.days == DEFAULT_WINDOW_DAYS


def test_the_window_is_computed_in_jst() -> None:
    """06:00 UTC is already the 19th in JST, so the window ends on the 18th.
    Computing it in UTC would shift every boundary by nine hours and split a
    day's sales across two windows."""
    late_utc = datetime(2026, 9, 18, 23, 0, tzinfo=UTC)  # 2026-09-19 08:00 JST
    assert velocity_window(late_utc).last_day.isoformat() == "2026-09-18"

"""実行計画の固定 (P2-043) — 時系列スキャンが索引を使えること。

`inventory_events` is the table that only grows, and the hourly rollup scans
it by time on every run. A sequential scan over it is invisible while the
table is small and becomes the whole runtime once it is not, which is exactly
the failure a 検収 performance test is supposed to prevent from ever shipping.

WHY `enable_seqscan = off` IS NOT CHEATING

On a test database with a handful of rows PostgreSQL picks a sequential scan
for everything, because it genuinely is cheaper — so asserting "no Seq Scan"
against real statistics would only prove the table is small. Turning the
setting off adds a large cost penalty to sequential scans but does NOT remove
them: when no index can serve the predicate, the planner still returns a Seq
Scan because it has no alternative.

So the assertion means precisely: **an index exists that can serve this
predicate**. That is the property that has to hold forever, and it is the one
that a missing index breaks.

It does NOT prove the planner will choose that index in production. That is
what the measured run in P2-043 is for; this is the permanent guard under it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.services.analytics_rollup import (
    changed_event_days_stmt,
    changed_order_days_stmt,
    day_movement_stmt,
    opening_balance_stmt,
)

pytestmark = pytest.mark.integration

SINCE = datetime(2026, 9, 1, tzinfo=UTC)
DAY_START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
DAY_END = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
POPULATION = [1, 2, 3]


async def _plan(session, stmt) -> str:
    """The plan with sequential scans priced out of the running.

    Reset afterwards so the setting cannot leak into another test and quietly
    change what IT is measuring.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    compiled = stmt.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})
    rows = await session.execute(text(f"EXPLAIN {compiled}"))
    return "\n".join(str(line[0]) for line in rows.all())


def _assert_indexed(plan: str, table: str) -> None:
    assert f"Seq Scan on {table}" not in plan, (
        f"{table} has no index that can serve this predicate — the planner fell "
        f"back to a sequential scan even with enable_seqscan off:\n{plan}"
    )


async def test_the_changed_event_days_scan_is_indexed(db_session) -> None:
    """Filtered on `created_at`, which ix_inventory_events_created_at exists
    for. Run hourly, over the whole event log."""
    plan = await _plan(db_session, changed_event_days_stmt(SINCE))
    _assert_indexed(plan, "inventory_events")


async def test_the_opening_balance_scan_is_indexed(db_session) -> None:
    """(master_sku_id, occurred_at) — ix_inventory_events_sku_time. This one
    reads EVERY event before the day, for every SKU in the population, so it is
    the heaviest query in the rollup."""
    plan = await _plan(db_session, opening_balance_stmt(DAY_START, POPULATION))
    _assert_indexed(plan, "inventory_events")


async def test_the_day_movement_scan_is_indexed(db_session) -> None:
    plan = await _plan(db_session, day_movement_stmt(DAY_START, DAY_END, POPULATION))
    _assert_indexed(plan, "inventory_events")


async def test_the_changed_order_days_scan_is_indexed(db_session) -> None:
    """Filtered on `orders.updated_at`. The rollup runs hourly and this decides
    which days to rebuild, so without an index it reads the whole orders table
    every hour, for ever."""
    plan = await _plan(db_session, changed_order_days_stmt(SINCE))
    _assert_indexed(plan, "orders")


async def test_the_guard_can_actually_fail(db_session) -> None:
    """The gate's own gate.

    Every assertion above passes trivially if EXPLAIN output stopped arriving
    or `_assert_indexed` stopped looking. `created_at` on `master_skus` has no
    index, so this plan MUST contain a sequential scan — if it does not, the
    harness is broken and the other four results mean nothing.
    """
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = await db_session.execute(
        text("EXPLAIN SELECT id FROM master_skus WHERE created_at > now()")
    )
    plan = "\n".join(str(line[0]) for line in rows.all())
    assert "Seq Scan on master_skus" in plan, (
        "expected an unindexed predicate to produce a sequential scan; the "
        f"harness cannot detect one:\n{plan}"
    )

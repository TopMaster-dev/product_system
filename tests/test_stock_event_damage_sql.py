"""The damage queries, compiled but not run.

An inspection that reports nothing is indistinguishable from a clean database,
so a query broken by a wrong join fails silently and reads as good news. The
integration tests seed real damage and prove it is found; these compile the SQL
here, without a database, so a malformed statement cannot reach a review as
"0件、問題なし".
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.cli.inspect_stock_event_damage import (
    unfanned_parent_events_stmt,
    unmanaged_events_stmt,
)

pytestmark = pytest.mark.unit


def _sql(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_the_unmanaged_query_selects_only_unmanaged_masters() -> None:
    sql = _sql(unmanaged_events_stmt())
    assert "is_stock_managed IS false" in sql
    assert "master_skus JOIN inventory_events" in sql


def test_the_unmanaged_query_looks_only_at_order_driven_events() -> None:
    """A manual adjustment on a 在庫管理対象外 SKU is a deliberate correction,
    not damage. Sweeping it in would turn the fix into the finding."""
    sql = _sql(unmanaged_events_stmt())
    assert "order_consumed" in sql
    assert "cancellation_returned" in sql
    assert "manual_adjust" not in sql


def test_the_parent_query_requires_the_absence_of_a_component_event() -> None:
    """The whole claim rests on this NOT EXISTS. Without it the query returns
    every bundle parent that ever sold, and the report becomes noise."""
    sql = _sql(unfanned_parent_events_stmt())
    assert "NOT (EXISTS" in sql


def test_the_parent_query_matches_the_component_on_the_order_line() -> None:
    """Matching on the SKU alone would clear a parent because some OTHER order
    of it happened to fan out correctly."""
    sql = _sql(unfanned_parent_events_stmt())
    for column in ("source_channel", "source_order_id", "source_line_id", "event_type"):
        assert sql.count(column) >= 2, column


def test_the_parent_query_ignores_masters_that_have_no_components() -> None:
    """`is_bundle` can be set before the components exist. Such a master has
    nothing to fan out to, so its own event is the correct one."""
    sql = _sql(unfanned_parent_events_stmt())
    assert "bundle_components" in sql
    assert "EXISTS (SELECT 1" in sql


def test_both_queries_aggregate_per_master() -> None:
    """The operator needs one row per SKU with a total, not one row per event —
    a per-event dump of a real backlog is unreadable."""
    for stmt in (unmanaged_events_stmt(), unfanned_parent_events_stmt()):
        sql = _sql(stmt)
        assert "GROUP BY" in sql
        assert "count(" in sql
        assert "sum(" in sql

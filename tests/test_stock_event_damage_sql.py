"""The damage queries, compiled but not run.

An inspection that reports nothing is indistinguishable from a clean database,
so a query broken by a wrong join fails silently and reads as good news. The
integration tests seed real damage and prove it is found; these compile the SQL
here, without a database, so a malformed statement cannot reach a review as
"0件、問題なし".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.cli.inspect_stock_event_damage import (
    DamagedSku,
    _print_section,
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


# --- 要対応か、決着済みか --------------------------------------------------


def _damaged(**kw: object) -> DamagedSku:
    base: dict[str, object] = {
        "master_sku_id": 1,
        "sku_code": "H1",
        "name": "ギフトラッピング",
        "note": "packaging",
        "events": 1965,
        "net_delta": -2043,
        "on_hand_qty": 0,
    }
    base.update(kw)
    return DamagedSku(**base)  # type: ignore[arg-type]


def test_a_sku_back_at_zero_is_settled() -> None:
    """The real H1. 1965 events, all pre-flag history, zeroed by a stocktake on
    2026-08-20. Nothing is outstanding."""
    assert _damaged().settled is True


def test_a_sku_still_holding_a_figure_is_not_settled() -> None:
    """An unmanaged SKU has no stock to hold. A non-zero figure is the thing
    somebody has to correct."""
    assert _damaged(on_hand_qty=-12).settled is False


def test_the_period_is_printed_so_pre_flag_history_is_recognisable() -> None:
    row = _damaged(
        first_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert row.period == "2026-01-01 〜 2026-08-20"


def test_a_row_with_no_period_prints_nothing_rather_than_none() -> None:
    assert _damaged().period == ""


def test_the_settled_rows_are_reported_apart_from_the_outstanding_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lesson from the first production run: 2027 settled events printed as
    one list read as a serious finding."""
    _print_section(
        "在庫管理対象外のマスタに書かれた受注イベント",
        "explanation",
        [_damaged(), _damaged(sku_code="BROKEN", on_hand_qty=-12)],
    )
    out = capsys.readouterr().out

    assert "要対応 1SKU" in out
    assert "決着済みのSKUが 1件" in out
    assert "対応不要" in out


def test_all_settled_reports_no_action_needed(capsys: pytest.CaptureFixture[str]) -> None:
    _print_section("t", "explanation", [_damaged()])
    out = capsys.readouterr().out

    assert "要対応なし" in out
    # The explanation belongs to a finding. Printing it over settled history is
    # what made the first run read as an alarm.
    assert "explanation" not in out

"""在庫一覧の動的閾値 (P2-017) — each row judged by its own number.

This file exists because of a bug that was written and caught during W8, and
that would not have failed anything.

The threshold became a SQL column so the badge counts, the row list and the sort
could all resolve it identically. The route then passed that ColumnElement into
the template, where the existing `{% if qty < threshold %}` compared an int
against a SQLAlchemy expression. That does not raise — it produces another
expression, which Jinja finds truthy — so EVERY row would have rendered as
低在庫, on a screen whose job is to show which few are.

So: rows carry `low_stock_threshold`, the macros take it as a parameter, and the
context variable named `threshold` is the scalar fallback used only by copy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.ui.deps import templates

pytestmark = pytest.mark.unit


def _row(
    master_id: int,
    code: str,
    qty: int,
    threshold: int,
    *,
    velocity: float | None = 1.0,
) -> dict[str, Any]:
    return {
        "id": master_id,
        "sku_code": code,
        "name": f"{code} の商品",
        "jan_code": None,
        "image_url": None,
        "archived_at": None,
        "is_stock_managed": True,
        "on_hand_qty": qty,
        "low_stock_threshold": threshold,
        "velocity_per_day": velocity,
        "updated_at": datetime(2026, 9, 28, tzinfo=UTC),
    }


def _context(**overrides: Any) -> dict[str, Any]:
    rows = overrides.pop("rows", None)
    if rows is None:
        # A fast mover held at 30 is LOW (threshold 70); a slow one held at 5 is
        # NORMAL (threshold 3). Same quantities, opposite verdicts — which is
        # the whole point of P2-017 and impossible to express with one number.
        rows = [_row(1, "FAST", 30, 70, velocity=5.0), _row(2, "SLOW", 5, 3, velocity=0.1)]
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "rows": rows,
        "counts": {"negative": 0, "zero": 0, "low": 1},
        "q": "",
        "filter_mode": "all",
        "sort": "status",
        "dir": "asc",
        "offset": 0,
        "best_sellers": set(),
        "threshold": 10,
        "include_hidden": 0,
        "flash": None,
        "pagination": {
            "total": len(rows),
            "offset": 0,
            "has_prev": False,
            "has_next": False,
            "qs_prev": "",
            "qs_next": "",
        },
    }
    base.update(overrides)
    return base


def _render(**overrides: Any) -> str:
    return templates.env.get_template("inventory_list.html").render(**_context(**overrides))


def test_the_page_renders() -> None:
    html = _render()
    assert "FAST" in html
    assert "SLOW" in html


def test_two_skus_at_different_rates_get_opposite_verdicts() -> None:
    """A SKU holding 30 is low while one holding 5 is normal, because the first
    sells fifty times faster. One global number cannot say this."""
    html = _render()
    low_marker = html.count("低在庫")
    assert low_marker >= 1
    # The slow SKU holds 5 against a threshold of 3, so it must NOT be flagged.
    slow_section = html.split("SLOW")[1][:600]
    assert "低在庫" not in slow_section


def test_each_rows_threshold_is_visible() -> None:
    """P2-017 is otherwise invisible: an operator seeing 30 flagged and 5 clear
    has no way to tell that is deliberate rather than broken."""
    html = _render()
    assert "閾値 70" in html
    assert "閾値 3" in html


def test_the_context_threshold_is_a_scalar_not_used_for_row_verdicts() -> None:
    """The regression guard. If the template ever reads the global for a row
    comparison again, a fallback far above both rows would flag everything."""
    html = _render(threshold=9999)
    slow_section = html.split("SLOW")[1][:600]
    assert "低在庫" not in slow_section


def test_the_filter_labels_no_longer_quote_a_fixed_range() -> None:
    """ "低在庫 (1〜9)" was true only while the threshold was 10 for every SKU."""
    html = _render()
    assert "販売速度に応じた閾値未満" in html
    assert "1〜9" not in html


def test_a_sku_with_no_velocity_row_still_renders() -> None:
    """The hours between deploy and the first rollup, and every newly created
    master. The fallback threshold arrives on the row from COALESCE."""
    html = _render(rows=[_row(3, "NEW", 0, 10, velocity=None)])
    assert "NEW" in html
    assert "閾値 10" in html


def test_negative_and_zero_still_outrank_low() -> None:
    html = _render(rows=[_row(4, "NEG", -3, 70), _row(5, "ZERO", 0, 70)])
    assert "マイナス" in html
    assert "ゼロ" in html


def test_an_empty_list_renders() -> None:
    html = _render(rows=[], total=0, counts={"negative": 0, "zero": 0, "low": 0})
    assert "FAST" not in html

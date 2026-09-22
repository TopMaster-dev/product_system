"""欠品リスク一覧 (P2-018) のレンダリング.

The screen exists so an operator can decide what to reorder first, which makes
one confusion expensive: a SKU with no forecast must not read as a safe one.
There are three reasons a row has no days-remaining — it never sells, it has too
little history, or it has already run out — and the last is the most urgent row
on the page. An em dash plus an explicit footnote, never a number.

The second thing held still is the warning about the stock baseline. Until the
October stocktake the numerator of every days-remaining is inherited from CROSS
MALL's ledger rather than counted, so the figures are arithmetically correct and
factually meaningless. The page says so at the top rather than in a footnote.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.analytics_query import Provenance
from app.services.timeframe import PRESET_DAYS, Period
from app.services.velocity import CONFIDENCE_LABELS, StockoutRisk, Velocity
from app.ui.deps import templates

pytestmark = pytest.mark.unit

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")
TODAY = date(2026, 9, 29)


def _risk(
    sku: str,
    on_hand: int,
    consumed: int = 56,
    days_observed: int = 28,
    *,
    master_id: int = 1,
) -> StockoutRisk:
    v = Velocity(master_id, consumed, days_observed, 28)
    return StockoutRisk(
        master_sku_id=master_id,
        sku_code=sku,
        name=f"{sku} の商品",
        on_hand_qty=on_hand,
        velocity=v,
        threshold=v.threshold(),
        days_remaining=v.days_remaining(on_hand),
        stockout_on=v.stockout_on(on_hand, today=TODAY),
    )


def _context(**overrides: Any) -> dict[str, Any]:
    risks = overrides.pop("risks", None)
    if risks is None:
        risks = [_risk("OUT", 0), _risk("SOON", 4, master_id=2), _risk("QUIET", 500, 0, 28)]
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "period": PERIOD,
        "presets": list(PRESET_DAYS),
        "cover_days": 14,
        "risks": risks,
        "source": Provenance(datetime(2026, 9, 29, tzinfo=UTC), date(2026, 7, 20), 0, 0.5),
        "labels": CONFIDENCE_LABELS,
        "limit": 100,
        "out_of_stock": sum(1 for r in risks if r.on_hand_qty <= 0),
        "below_threshold": sum(1 for r in risks if r.is_below_threshold),
        "unforecastable": sum(1 for r in risks if r.days_remaining is None),
    }
    base.update(overrides)
    return base


def _render(**overrides: Any) -> str:
    return templates.env.get_template("analytics_stockout_risk.html").render(
        **_context(**overrides)
    )


def test_the_page_renders() -> None:
    html = _render()
    assert "欠品リスク一覧" in html
    assert "OUT" in html
    assert "SOON" in html


def test_the_baseline_warning_is_at_the_top_not_in_a_footnote() -> None:
    """Every days-remaining divides a number inherited from CROSS MALL's ledger.
    A reader who misses this acts on a figure that is arithmetically right and
    factually meaningless."""
    html = _render()
    assert "残日数は在庫数が正しいことを前提にしています" in html
    assert "並び順の目安" in html


def test_velocity_is_stated_as_unaffected_by_the_baseline() -> None:
    """It comes from consumption events, so it is trustworthy even while the
    stock figure is not — and that distinction is the whole value of the page
    before the stocktake."""
    assert "在庫数の影響を受けません" in _render()


def test_a_sku_with_no_forecast_shows_a_dash_and_the_note_says_it_is_not_safe() -> None:
    """The expensive confusion. A number there would sort and read as safety."""
    html = _render()
    assert "安全という意味ではありません" in html


def test_an_out_of_stock_row_says_so_rather_than_showing_a_date() -> None:
    """It did not run out on some future date. It has run out."""
    assert "在庫切れ" in _render()


def test_confidence_is_shown_per_row() -> None:
    """A rate from four days and a rate from four weeks look identical as
    numbers. The badge is what separates them."""
    html = _render(risks=[_risk("NEW", 10, consumed=40, days_observed=4)])
    assert CONFIDENCE_LABELS[_risk("NEW", 10, 40, 4).velocity.confidence] in html
    assert "判定不可" in html


def test_a_well_observed_row_reads_as_sufficient() -> None:
    assert "十分" in _render()


def test_the_dynamic_threshold_is_shown_next_to_the_stock() -> None:
    """P2-017 is invisible unless the operator can see that a fast SKU is
    flagged at a higher number than a slow one."""
    html = _render(risks=[_risk("FAST", 10, consumed=140)])
    assert "/ 70" in html


def test_negative_stock_is_marked() -> None:
    html = _render(risks=[_risk("NEG", -5)])
    assert "text-red-600" in html


def test_an_empty_list_renders() -> None:
    html = _render(risks=[])
    assert "対象のSKUがありません" in html


def test_the_page_states_it_is_truncated_and_where_the_rest_is() -> None:
    """A hundred rows of six hundred, silently, would be summed by someone."""
    html = _render()
    assert "上位100件" in html
    assert "CSVでご確認ください" in html


def test_the_export_link_carries_the_current_settings() -> None:
    html = _render(cover_days=7)
    assert "cover=7" in html
    assert "period=28d" in html

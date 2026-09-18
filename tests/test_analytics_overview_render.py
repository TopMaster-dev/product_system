"""The overview template must render, including on the days it is hardest to.

A Jinja error here is a 500 on the dashboard, and `env.get_template()` will not
find one: undefined filters, wrong macro arity and attribute typos all survive
compilation and fail at render. The integration suite needs PostgreSQL and does
not run locally, so this is the only guard before production.

The contexts below are the awkward ones rather than the happy path — a brand-new
shop with no rollups at all, a period reaching back before the data starts, and
every warning band lit at once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services.analytics_query import Delta, Kpis, Provenance
from app.services.timeframe import PRESET_DAYS, Period
from app.ui import charts
from app.ui.deps import templates

pytestmark = pytest.mark.unit

TEMPLATE = "analytics_overview.html"
PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")


def _kpis(**kw: Any) -> Kpis:
    base: dict[str, Any] = {
        "gross_sales_jpy": Decimal(1_234_567),
        "sold_quantity": 412,
        "order_count": 205,
        "unmapped_sales_jpy": Decimal(10_000),
        "total_on_hand_qty": 5_120,
        "out_of_stock_sku_count": 8,
        "sku_count": 624,
        "average_on_hand_qty": 5_000.0,
        "days_with_data": 28,
    }
    return Kpis(**{**base, **kw})


def _provenance(**kw: Any) -> Provenance:
    base: dict[str, Any] = {
        "last_rollup_at": datetime(2026, 9, 29, 3, 0, tzinfo=UTC),
        "data_start": date(2026, 7, 20),
        "snapshot_drift_count": 0,
        "stale_hours": 0.5,
    }
    return Provenance(**{**base, **kw})


def _context(**overrides: Any) -> dict[str, Any]:
    kpis = overrides.pop("kpis", None) or _kpis()
    labels = [d.strftime("%m/%d") for d in PERIOD.dates()]
    values = [float(i) for i in range(len(labels))]
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "period": PERIOD,
        "previous": PERIOD.previous(),
        "presets": list(PRESET_DAYS),
        "kpis": kpis,
        "source": _provenance(),
        "trend": charts.line_chart(
            labels, [("当期間", values, "#4F46E5"), ("前期間", values, "#CBD5E1")]
        ),
        "shares": charts.share_bars([("rakuten", 600.0), ("shopify", 300.0)]),
        "deltas": {
            "sales": Delta.of(100, 80),
            "quantity": Delta.of(10, 0),
            "orders": Delta.of(5, 5),
        },
    }
    base.update(overrides)
    return base


def _render(**overrides: Any) -> str:
    return templates.env.get_template(TEMPLATE).render(**_context(**overrides))


def test_the_ordinary_page_renders() -> None:
    html = _render()
    assert "分析ダッシュボード" in html
    assert "売上推移" in html
    assert "チャネル別構成" in html


def test_a_zero_baseline_renders_an_em_dash_not_a_percentage() -> None:
    """Delta.of(10, 0).change is None. The tile must say "—"; +100% would be a
    figure the client quotes back."""
    html = _render()
    assert "前期間比 —" in html


def test_a_brand_new_shop_renders() -> None:
    """No rollups, no sales, no stock. Every number is zero or None and the
    charts are empty — the state the dashboard is in on day one."""
    html = _render(
        kpis=_kpis(
            gross_sales_jpy=Decimal(0),
            sold_quantity=0,
            order_count=0,
            unmapped_sales_jpy=Decimal(0),
            total_on_hand_qty=0,
            out_of_stock_sku_count=0,
            sku_count=0,
            average_on_hand_qty=0.0,
            days_with_data=0,
        ),
        source=_provenance(last_rollup_at=None, data_start=None, stale_hours=None),
        trend=charts.line_chart([], []),
        shares=charts.share_bars([]),
        deltas={"sales": Delta.of(0, 0), "quantity": Delta.of(0, 0), "orders": Delta.of(0, 0)},
    )
    assert "この期間の売上データはまだありません" in html
    assert "この期間の売上はありません" in html


def test_snapshot_drift_takes_the_banner_over_staleness() -> None:
    """Both can be true at once. Drift wins: stale numbers are merely old, while
    drifted ones disagree with the event log they came from."""
    html = _render(source=_provenance(snapshot_drift_count=12, stale_hours=9.0))
    assert "在庫数とイベント履歴が一致していません" in html
    assert "更新されていません" not in html


def test_staleness_shows_when_there_is_no_drift() -> None:
    html = _render(source=_provenance(snapshot_drift_count=0, stale_hours=9.0))
    assert "9 時間更新されていません" in html


def test_material_unmapped_revenue_is_called_out() -> None:
    html = _render(
        kpis=_kpis(gross_sales_jpy=Decimal(800), unmapped_sales_jpy=Decimal(200)),
    )
    assert "20.0% が商品未特定です" in html


def test_an_immaterial_unmapped_share_stays_quiet() -> None:
    html = _render(kpis=_kpis(gross_sales_jpy=Decimal(990), unmapped_sales_jpy=Decimal(10)))
    assert "が商品未特定です" not in html


def test_a_window_reaching_before_the_data_starts_says_so() -> None:
    """A 28-day window over 10 days of history. Without the note, the empty
    stretch reads as a collapse in trade rather than an absence of records."""
    html = _render(kpis=_kpis(days_with_data=10))
    assert "集計開始前のため数値がありません" in html


def test_a_complete_window_does_not_warn() -> None:
    html = _render(kpis=_kpis(days_with_data=28))
    assert "集計開始前のため数値がありません" not in html


def test_the_selected_preset_is_marked() -> None:
    html = _render()
    assert "?period=28d" in html
    assert "?period=365d" in html


def test_stock_tiles_state_they_are_closing_balances() -> None:
    """The one number on this page a reader could mistake for a period total."""
    html = _render()
    assert "期間末時点の残高" in html

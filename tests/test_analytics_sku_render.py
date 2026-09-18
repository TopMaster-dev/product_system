"""SKU別推移 (P2-007 / P2-008) と カテゴリ別売上 (P2-010) のレンダリング.

The states worth holding still are the ones where an empty chart would lie.

A set parent and a 在庫管理対象外 item hold no rows in `sku_daily_stock`, so
their stock chart is empty — and an empty chart reads as "zero stock", which for
a set parent whose availability derives from its components is the opposite of
the truth. A day with no stock row is rendered as an em dash rather than 0, for
the same reason: zero would invent an out-of-stock day.

On the category screen, the 未分類 row is always present and the shares are
always against the FULL total. Rescaling to the categorised subset would make
every category look larger exactly while the client is still filling the sheet.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services.analytics_query import CategorySales, SalesFilter, SkuDay, SkuProfile
from app.services.timeframe import PRESET_DAYS, Period
from app.ui import charts
from app.ui.deps import templates

pytestmark = pytest.mark.unit

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 3), "7d")


def _profile(**kw: Any) -> SkuProfile:
    base: dict[str, Any] = {
        "master_sku_id": 1,
        "sku_code": "N108gold",
        "name": "アンカーネックレス 約53cm",
        "is_bundle": False,
        "is_stock_managed": True,
        "archived_at": None,
        "category_name": "ネックレス",
    }
    return SkuProfile(**{**base, **kw})


def _days(on_hand: list[int | None]) -> list[SkuDay]:
    return [
        SkuDay(day, qty, 0, 2, Decimal(2000))
        for day, qty in zip(PERIOD.dates(), on_hand, strict=True)
    ]


def _sku_context(**overrides: Any) -> dict[str, Any]:
    days = overrides.pop("days", None) or _days([100, 98, 96])
    labels = [d.stat_date.strftime("%m/%d") for d in days]
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "period": PERIOD,
        "presets": list(PRESET_DAYS),
        "profile": _profile(),
        "days": days,
        "missing_stock_days": sum(1 for d in days if d.on_hand_qty is None),
        "stock_chart": charts.line_chart(labels, [("在庫数", [100.0, 98.0, 96.0], "#0EA5E9")]),
        "sales_chart": charts.line_chart(labels, [("売上高", [2000.0] * len(days), "#4F46E5")]),
        "period_quantity": sum(d.quantity for d in days),
        "period_sales": sum((d.gross_sales_jpy for d in days), start=Decimal(0)),
    }
    base.update(overrides)
    return base


def _render_sku(**overrides: Any) -> str:
    return templates.env.get_template("analytics_sku.html").render(**_sku_context(**overrides))


def test_the_sku_page_renders() -> None:
    html = _render_sku()
    assert "N108gold" in html
    assert "在庫数の推移" in html
    assert "売上高の推移" in html


def test_a_set_parent_explains_its_empty_stock_chart() -> None:
    """Otherwise a flat line at zero reads as out of stock, when in fact the
    stock lives on its components."""
    html = _render_sku(profile=_profile(is_bundle=True))
    assert "構成品側で管理されています" in html
    assert "在庫ゼロを意味しません" in html


def test_a_non_inventory_item_explains_itself() -> None:
    html = _render_sku(profile=_profile(is_stock_managed=False))
    assert "在庫管理の対象外" in html


def test_an_archived_sku_is_marked_but_still_charted() -> None:
    """Archiving hides a SKU from current-state screens; its history is exactly
    what someone opening this page came for."""
    html = _render_sku(profile=_profile(archived_at=datetime(2026, 7, 20)))
    assert "アーカイブ済" in html
    assert "在庫管理の対象外" not in html


def test_a_day_with_no_stock_row_shows_a_dash_not_a_zero() -> None:
    html = _render_sku(days=_days([100, None, 96]))
    assert "在庫の記録がありません" in html


def test_a_complete_series_does_not_warn() -> None:
    assert "在庫の記録がありません" not in _render_sku()


def test_negative_stock_is_marked() -> None:
    """26 masters registered on 2026-09-17 sit at zero and go negative until
    the October stocktake. The client was told; the screen should agree."""
    html = _render_sku(days=_days([2, -1, -4]))
    assert "text-red-600" in html


def test_an_uncategorised_sku_reads_as_unclassified() -> None:
    html = _render_sku(profile=_profile(category_name=None))
    assert "未分類" in html


# --- category screen ------------------------------------------------------


def _cat_context(**overrides: Any) -> dict[str, Any]:
    rows = overrides.pop(
        "rows",
        [
            CategorySales(
                1,
                "ネックレス",
                4,
                Decimal(400),
                [CategorySales(2, "チェーン", 10, Decimal(1000))],
            ),
            CategorySales(None, "未分類", 1, Decimal(100)),
        ],
    )
    total = sum((r.total_sales_jpy for r in rows), start=Decimal(0))
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "period": PERIOD,
        "presets": list(PRESET_DAYS),
        "channels": ["rakuten", "shopify"],
        "filter": SalesFilter(),
        "rows": rows,
        "total": total,
        "shares": charts.share_bars([(r.name, float(r.total_sales_jpy)) for r in rows]),
    }
    base.update(overrides)
    return base


def _render_cat(**overrides: Any) -> str:
    return templates.env.get_template("analytics_categories.html").render(
        **_cat_context(**overrides)
    )


def test_the_category_page_renders_with_children_indented() -> None:
    html = _render_cat()
    assert "ネックレス" in html
    assert "チェーン" in html
    assert "カテゴリ別売上" in html


def test_shares_are_against_the_full_total_including_unclassified() -> None:
    """1500 total; the parent rolls up to 1400, which is 93.3%. Rescaling to the
    categorised subset would report 100% and hide the gap."""
    html = _render_cat()
    assert "93.3%" in html


def test_with_only_the_unclassified_row_the_screen_says_why() -> None:
    """The state before the client has registered any categories. An empty
    table with no explanation reads as a broken screen."""
    html = _render_cat(rows=[CategorySales(None, "未分類", 5, Decimal(500))])
    assert "カテゴリが未登録のため" in html


def test_a_large_uncategorised_share_is_called_out() -> None:
    html = _render_cat(
        rows=[
            CategorySales(1, "ネックレス", 1, Decimal(100)),
            CategorySales(None, "未分類", 9, Decimal(900)),
        ]
    )
    assert "90.0% がカテゴリ未設定です" in html


def test_a_small_uncategorised_share_stays_quiet() -> None:
    html = _render_cat(
        rows=[
            CategorySales(1, "ネックレス", 99, Decimal(9900)),
            CategorySales(None, "未分類", 1, Decimal(100)),
        ]
    )
    assert "がカテゴリ未設定です" not in html


def test_an_empty_period_does_not_divide_by_zero() -> None:
    html = _render_cat(rows=[CategorySales(None, "未分類", 0, Decimal(0))], shares=[])
    assert "カテゴリが未登録のため" in html

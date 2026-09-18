"""売上明細 — bucketing, the three-state category filter, and the template.

Two things here are easy to get quietly wrong.

**Bucketing happens in Python**, through `timeframe.bucket`, so the screens and
the exports share one definition of a week. Doing it in SQL would work today and
diverge the moment anything else rounds a date.

**The category filter has three states, not two.** A number selects a category,
absence means no filter, and "none" selects the SKUs that have none. Collapsing
the last two hides exactly the rows the client needs while they are still
filling the category sheet in.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.services.analytics_query import BucketRow, SalesFilter, SkuRow, bucketed_sales
from app.services.timeframe import PRESET_DAYS, Period
from app.ui import charts
from app.ui.deps import templates

pytestmark = pytest.mark.unit

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")


# --- the three-state filter ----------------------------------------------


def test_no_filter_produces_no_conditions() -> None:
    assert SalesFilter().conditions() == []
    assert SalesFilter().is_narrowed is False


def test_unclassified_is_a_filter_not_the_absence_of_one() -> None:
    """`category_id IS NULL` is a real narrowing. Treating "show me the
    uncategorised" as "show me everything" would make the screen look broken
    exactly when the client is using it to find gaps."""
    f = SalesFilter(unclassified_only=True)
    assert len(f.conditions()) == 1
    assert f.is_narrowed is True


def test_unclassified_wins_over_a_stale_category_id() -> None:
    """Both can arrive together from a hand-edited URL. Applying both would
    produce `category_id = 4 AND category_id IS NULL` — always empty, with no
    explanation on screen."""
    f = SalesFilter(category_id=4, unclassified_only=True)
    assert len(f.conditions()) == 1


def test_channel_and_category_combine() -> None:
    assert len(SalesFilter(channel="rakuten", category_id=2).conditions()) == 2


# --- bucketing ------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._rows)


DAYS = [
    (date(2026, 9, 7), 2, Decimal(200), 2),  # Monday
    (date(2026, 9, 8), 3, Decimal(300), 3),  # Tuesday
    (date(2026, 9, 14), 5, Decimal(500), 4),  # the following Monday
]


async def test_daily_buckets_pass_through() -> None:
    rows = await bucketed_sales(_FakeSession(DAYS), PERIOD)  # type: ignore[arg-type]
    assert [r.bucket for r in rows] == [d[0] for d in DAYS]


async def test_weekly_buckets_start_on_monday() -> None:
    """Japanese retail weeks run Monday to Sunday, and so does the client's own
    reporting. The 7th and 8th fall in one week; the 14th starts the next."""
    rows = await bucketed_sales(_FakeSession(DAYS), PERIOD, granularity="week")  # type: ignore[arg-type]
    assert rows == [
        BucketRow(date(2026, 9, 7), 5, Decimal(500), 5),
        BucketRow(date(2026, 9, 14), 5, Decimal(500), 4),
    ]


async def test_monthly_buckets_collapse_to_the_first() -> None:
    rows = await bucketed_sales(_FakeSession(DAYS), PERIOD, granularity="month")  # type: ignore[arg-type]
    assert rows == [BucketRow(date(2026, 9, 1), 10, Decimal(1000), 9)]


async def test_an_unknown_granularity_falls_back_to_days() -> None:
    """`timeframe.bucket` returns the day unchanged rather than raising, because
    these values arrive from query strings."""
    rows = await bucketed_sales(_FakeSession(DAYS), PERIOD, granularity="fortnight")  # type: ignore[arg-type]
    assert len(rows) == 3


async def test_buckets_come_back_in_date_order() -> None:
    shuffled = [DAYS[2], DAYS[0], DAYS[1]]
    rows = await bucketed_sales(_FakeSession(shuffled), PERIOD, granularity="week")  # type: ignore[arg-type]
    assert [r.bucket for r in rows] == [date(2026, 9, 7), date(2026, 9, 14)]


async def test_an_empty_period_buckets_to_nothing() -> None:
    assert await bucketed_sales(_FakeSession([]), PERIOD) == []  # type: ignore[arg-type]


# --- template -------------------------------------------------------------


class _Cat:
    def __init__(self, cid: int, name: str) -> None:
        self.id, self.name = cid, name
        self.is_active = True


class _Node:
    def __init__(self, cid: int, name: str, children: list[Any] | None = None) -> None:
        self.category = _Cat(cid, name)
        self.children = children or []
        self.own_sku_count = 3

    @property
    def total_sku_count(self) -> int:
        return self.own_sku_count


class _Overview:
    def __init__(self, roots: list[Any], total: int, unclassified: int = 12) -> None:
        self.roots = roots
        self.total_categories = total
        self.unclassified_count = unclassified


def _context(**overrides: Any) -> dict[str, Any]:
    buckets = [BucketRow(date(2026, 9, 7), 5, Decimal(500), 5)]
    base: dict[str, Any] = {
        "operator": "tester",
        "version": "test",
        "period": PERIOD,
        "presets": list(PRESET_DAYS),
        "granularity": "day",
        "granularities": (("day", "日次"), ("week", "週次"), ("month", "月次")),
        "channels": ["rakuten", "shopify"],
        "categories": _Overview([_Node(1, "ネックレス", [_Node(2, "チェーン")])], total=2),
        "filter": SalesFilter(),
        "selected_category": "",
        "unclassified_value": "none",
        "buckets": buckets,
        "rows": [
            SkuRow(1, "N108gold", "アンカーネックレス", "ネックレス", 12, Decimal(120_000)),
            SkuRow(2, "B73silver17", "ダブルフックブレス", None, 4, Decimal(40_000)),
        ],
        "trend": charts.line_chart(["09/07"], [("売上高", [500.0], "#4F46E5")]),
        "period_quantity": 5,
        "period_sales": Decimal(500),
    }
    base.update(overrides)
    return base


def _render(**overrides: Any) -> str:
    return templates.env.get_template("analytics_sales.html").render(**_context(**overrides))


def test_the_page_renders() -> None:
    html = _render()
    assert "売上明細" in html
    assert "N108gold" in html
    assert "売上上位SKU" in html


def test_a_sku_with_no_category_reads_as_unclassified_not_blank() -> None:
    assert "未分類" in _render()


def test_an_empty_result_explains_itself() -> None:
    html = _render(rows=[], buckets=[], trend=charts.line_chart([], []), period_sales=Decimal(0))
    assert "条件に一致する売上はありません" in html


def test_with_no_categories_registered_the_screen_says_why() -> None:
    """P2-010 ships last precisely so this state is survivable. An empty
    dropdown with no explanation reads as a broken filter."""
    html = _render(categories=_Overview([], total=0))
    assert "カテゴリが未登録のため" in html


def test_registered_categories_do_not_trigger_the_notice() -> None:
    assert "カテゴリが未登録のため" not in _render()


def test_child_categories_are_offered_under_their_parent() -> None:
    html = _render()
    assert "ネックレス / チェーン" in html


def test_a_narrowed_view_offers_a_way_out() -> None:
    html = _render(filter=SalesFilter(channel="rakuten"))
    assert "絞込を解除" in html


def test_an_unnarrowed_view_does_not() -> None:
    assert "絞込を解除" not in _render()


def test_the_footnote_warns_the_table_excludes_unmapped_sales() -> None:
    """Otherwise the top-SKU total not matching the overview reads as a bug."""
    html = _render()
    assert "未マッピング" in html

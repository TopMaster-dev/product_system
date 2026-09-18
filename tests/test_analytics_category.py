"""カテゴリ別売上 (P2-010) と SKU別推移 (P2-007 / P2-008).

The category rollup has one ordering hazard worth stating plainly. Rows come
back ordered by revenue, so a 中分類 can arrive before its 大分類. A single pass
that creates a placeholder for the missing parent and then merges the parent's
own row into it will discard whichever arrived second — and the loss is
invisible, because the total still looks plausible.

The other rule is the one this whole module keeps repeating: the 未分類 row is
always present, including at zero. Its absence reads as "everything is
categorised", which during the client's category rollout is exactly wrong.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.services.analytics_query import (
    UNCLASSIFIED_LABEL,
    CategorySales,
    SalesFilter,
    SkuProfile,
    category_sales,
    sku_series,
)
from app.services.timeframe import Period

pytestmark = pytest.mark.unit

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 3), "7d")


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def one(self) -> Any:
        return self._rows[0]

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = answers

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


# (id, name, parent_id, parent_name, quantity, sales)
CHILD_FIRST = [
    (2, "チェーン", 1, "ネックレス", 10, Decimal(1000)),
    (1, "ネックレス", None, None, 4, Decimal(400)),
]


async def test_a_parents_own_sales_survive_when_its_child_is_listed_first() -> None:
    """The ordering hazard. Revenue-ordered rows put the child on top, and a
    one-pass merge drops the 400 the parent sold directly."""
    rows = await category_sales(
        _FakeSession([CHILD_FIRST, [(0, Decimal(0))]]),  # type: ignore[arg-type]
        PERIOD,
    )
    parent = next(r for r in rows if r.category_id == 1)
    assert parent.gross_sales_jpy == Decimal(400)
    assert parent.total_sales_jpy == Decimal(1400)
    assert [c.category_id for c in parent.children] == [2]


async def test_the_same_result_when_the_parent_comes_first() -> None:
    rows = await category_sales(
        _FakeSession([list(reversed(CHILD_FIRST)), [(0, Decimal(0))]]),  # type: ignore[arg-type]
        PERIOD,
    )
    parent = next(r for r in rows if r.category_id == 1)
    assert parent.total_sales_jpy == Decimal(1400)


async def test_a_parent_that_sold_nothing_still_holds_its_children() -> None:
    """It produced no row of its own. Dropping it would move its children's
    revenue out of the top level and the shares would stop summing."""
    rows = await category_sales(
        _FakeSession(  # type: ignore[arg-type]
            [[(2, "チェーン", 1, "ネックレス", 10, Decimal(1000))], [(0, Decimal(0))]]
        ),
        PERIOD,
    )
    parent = next(r for r in rows if r.category_id == 1)
    assert parent.gross_sales_jpy == Decimal(0)
    assert parent.total_sales_jpy == Decimal(1000)


async def test_the_unclassified_row_is_present_even_at_zero() -> None:
    rows = await category_sales(
        _FakeSession([[], [(0, Decimal(0))]]),  # type: ignore[arg-type]
        PERIOD,
    )
    assert [r.name for r in rows] == [UNCLASSIFIED_LABEL]
    assert rows[-1].category_id is None


async def test_the_unclassified_row_is_last_and_carries_its_total() -> None:
    rows = await category_sales(
        _FakeSession([CHILD_FIRST, [(7, Decimal(700))]]),  # type: ignore[arg-type]
        PERIOD,
    )
    assert rows[-1].name == UNCLASSIFIED_LABEL
    assert rows[-1].gross_sales_jpy == Decimal(700)


async def test_top_level_rows_are_ordered_by_rolled_up_revenue() -> None:
    """A 大分類 whose own sales are small but whose children are large must
    outrank one that merely sold more directly."""
    rows = await category_sales(
        _FakeSession(  # type: ignore[arg-type]
            [
                [
                    (3, "リング", None, None, 5, Decimal(500)),
                    (2, "チェーン", 1, "ネックレス", 10, Decimal(1000)),
                    (1, "ネックレス", None, None, 1, Decimal(100)),
                ],
                [(0, Decimal(0))],
            ]
        ),
        PERIOD,
    )
    assert [r.category_id for r in rows] == [1, 3, None]


def test_rolled_up_totals_include_the_node_itself() -> None:
    node = CategorySales(1, "親", 2, Decimal(200), [CategorySales(2, "子", 3, Decimal(300))])
    assert node.total_quantity == 5
    assert node.total_sales_jpy == Decimal(500)


def test_a_channel_filter_still_applies_to_the_unclassified_row() -> None:
    """Otherwise the uncategorised figure would be shop-wide while every other
    row was one channel, and the column would not sum."""
    f = SalesFilter(channel="rakuten", category_id=9)
    assert len(f.conditions_except_category()) == 1
    assert len(f.conditions()) == 2


# --- SKU series -----------------------------------------------------------


def _profile(**kw: Any) -> SkuProfile:
    base: dict[str, Any] = {
        "master_sku_id": 1,
        "sku_code": "N108gold",
        "name": "アンカーネックレス",
        "is_bundle": False,
        "is_stock_managed": True,
        "archived_at": None,
        "category_name": None,
    }
    return SkuProfile(**{**base, **kw})


def test_an_ordinary_sku_tracks_stock() -> None:
    p = _profile()
    assert p.tracks_stock is True
    assert p.no_stock_reason is None


def test_a_set_parent_explains_its_empty_stock_chart() -> None:
    """Its availability derives from components, so `sku_daily_stock` holds
    nothing for it. A flat line at zero would read as "out of stock"."""
    p = _profile(is_bundle=True)
    assert p.tracks_stock is False
    assert "構成品側" in (p.no_stock_reason or "")


def test_a_non_inventory_item_explains_itself_too() -> None:
    p = _profile(is_stock_managed=False)
    assert "在庫管理の対象外" in (p.no_stock_reason or "")


def test_an_archived_sku_still_tracks_stock() -> None:
    """Archiving is a visibility concept, not an inventory one. Its history is
    exactly what someone opening this screen is looking for."""
    p = _profile(archived_at=datetime(2026, 7, 20))
    assert p.tracks_stock is True


async def test_every_day_in_the_window_gets_a_row() -> None:
    """Quiet days are filled in rather than skipped. A stock line that omitted
    them would compress three still weeks into one step."""
    days = await sku_series(
        _FakeSession(  # type: ignore[arg-type]
            [
                [(date(2026, 9, 1), 100, 2)],  # stock: only the first day moved
                [(date(2026, 9, 3), 4, Decimal(400))],  # sales: only the third
            ]
        ),
        1,
        PERIOD,
    )
    assert [d.stat_date for d in days] == PERIOD.dates()
    assert [d.quantity for d in days] == [0, 0, 4]


async def test_a_day_with_no_stock_row_is_none_not_zero() -> None:
    """None means "not recorded"; zero means "held none". Rendering the first as
    the second invents an out-of-stock day."""
    days = await sku_series(_FakeSession([[], []]), 1, PERIOD)  # type: ignore[arg-type]
    assert all(d.on_hand_qty is None for d in days)

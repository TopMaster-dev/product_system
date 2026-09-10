"""The Shopify stock audit — the check that replaces CROSS MALL.

One rule dominates this file: **absent is not zero.**

The audit compares our stock against Shopify's and files the differences into
the approval queue, where an operator applies them. If a SKU Shopify does not
report were treated as zero, the queue would fill with proposals to empty the
stock of every item sold only on Rakuten — and each would look like a routine
correction, because that is exactly what the queue is for.

The rule is enforced twice, so both are tested here. The adapter drops a variant
with no `inventoryLevel` at the location rather than reading it as 0, and the
CLI only ever compares masters that Shopify actually reported.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cli.audit_shopify_stock import aggregate_levels

pytestmark = pytest.mark.unit


# --- aggregation ---------------------------------------------------------


def test_one_sku_on_several_variants_is_summed_and_counted() -> None:
    """Two variants sharing a SKU are two inventory items in Shopify but one
    master here. Summing without reporting would show up later only as an
    unexplained difference."""
    levels = [
        {"sku": "N23gold", "on_hand": 3},
        {"sku": "N23gold", "on_hand": 5},
        {"sku": "R41silver", "on_hand": 2},
    ]
    totals, duplicates = aggregate_levels(levels)
    assert totals == {"N23gold": 8, "R41silver": 2}
    assert duplicates == 1


def test_nothing_reported_aggregates_to_nothing() -> None:
    assert aggregate_levels([]) == ({}, 0)


def test_a_reported_zero_is_kept() -> None:
    """A SKU Shopify says it holds none of is a real difference worth showing.
    Only an UNREPORTED SKU is skipped — that is the whole distinction."""
    totals, _ = aggregate_levels([{"sku": "B09goldanklet", "on_hand": 0}])
    assert totals == {"B09goldanklet": 0}


# --- adapter parsing -----------------------------------------------------


class _FakeShopify:
    """Drives ShopifyAdapter.fetch_stock_levels without a network."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    async def _resolve_location_id(self) -> str:
        return "gid://shopify/Location/1"

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(variables)
        return self._pages[len(self.calls) - 1]


def _page(nodes: list[dict[str, Any]], *, cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                "edges": [{"node": n} for n in nodes],
            }
        }
    }


def _variant(
    sku: str,
    on_hand: int | None,
    available: int | None = None,
    *,
    tracked: bool = True,
) -> dict[str, Any]:
    if on_hand is None:
        level = None
    else:
        level = {
            "quantities": [
                {"name": "on_hand", "quantity": on_hand},
                {"name": "available", "quantity": on_hand if available is None else available},
            ]
        }
    return {
        "sku": sku,
        "inventoryItem": {
            "id": f"gid://ii/{sku}",
            "tracked": tracked,
            "inventoryLevel": level,
        },
    }


async def _fetch(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.adapters.shopify import ShopifyAdapter

    fake = _FakeShopify(pages)
    return await ShopifyAdapter.fetch_stock_levels(fake)  # type: ignore[arg-type]


async def test_a_variant_shopify_does_not_stock_here_is_dropped_not_zeroed() -> None:
    """The rule, at its source. Reading a missing inventoryLevel as 0 would
    propose emptying that SKU."""
    rows = await _fetch([_page([_variant("A1", 4), _variant("A2", None)])])
    assert [r["sku"] for r in rows] == ["A1"]


async def test_a_blank_sku_is_dropped() -> None:
    """Shopify permits variants with no SKU; they match no master and are not
    ours to audit."""
    rows = await _fetch([_page([_variant("", 9), _variant("  ", 9), _variant("A1", 1)])])
    assert [r["sku"] for r in rows] == ["A1"]


async def test_an_explicit_zero_survives() -> None:
    rows = await _fetch([_page([_variant("A1", 0)])])
    assert rows == [
        {
            "sku": "A1",
            "on_hand": 0,
            "available": 0,
            "tracked": True,
            "inventory_item_id": "gid://ii/A1",
        }
    ]


async def test_both_quantities_are_carried() -> None:
    """`available` is on_hand minus what is committed to unfulfilled orders.
    Which one our snapshots correspond to is decided from data, not assumed, so
    the fetch must not throw either away."""
    rows = await _fetch([_page([_variant("A1", 10, available=7)])])
    assert rows[0]["on_hand"] == 10
    assert rows[0]["available"] == 7


async def test_pagination_follows_the_cursor_to_the_end() -> None:
    rows = await _fetch(
        [
            _page([_variant("A1", 1)], cursor="c1"),
            _page([_variant("A2", 2)], cursor="c2"),
            _page([_variant("A3", 3)]),
        ]
    )
    assert [r["sku"] for r in rows] == ["A1", "A2", "A3"]


async def test_duplicate_skus_are_returned_separately_for_the_caller_to_judge() -> None:
    """The adapter does not sum. Combining two inventory items is a policy call
    that has to be visible, not buried in a fetch."""
    rows = await _fetch([_page([_variant("A1", 3), _variant("A1", 5)])])
    assert [r["on_hand"] for r in rows] == [3, 5]


async def test_the_location_is_resolved_once_and_passed_to_every_page() -> None:
    fake = _FakeShopify([_page([_variant("A1", 1)], cursor="c1"), _page([_variant("A2", 2)])])
    from app.adapters.shopify import ShopifyAdapter

    await ShopifyAdapter.fetch_stock_levels(fake)  # type: ignore[arg-type]
    assert [c["locId"] for c in fake.calls] == ["gid://shopify/Location/1"] * 2
    assert [c["cursor"] for c in fake.calls] == [None, "c1"]


# --- diagnosing a large difference set ------------------------------------


def test_describe_reports_direction_and_magnitude() -> None:
    """552 differences out of 603 is not a stock problem until the shape says
    so. All in one direction points at a systematic cause — the wrong quantity
    compared, or two numbers never synchronised — not at drift."""
    from app.cli.audit_shopify_stock import DiffInput, describe

    diffs = [
        DiffInput(master_sku_id=1, current_qty=2, target_qty=5),  # Shopify +3
        DiffInput(master_sku_id=2, current_qty=9, target_qty=10),  # Shopify +1
        DiffInput(master_sku_id=3, current_qty=4, target_qty=1),  # Shopify -3
    ]
    shape = describe(diffs)
    assert shape["diffs"] == 3
    assert shape["shopify_higher"] == 2
    assert shape["shopify_lower"] == 1
    assert shape["max_abs"] == 3


def test_describe_of_nothing_does_not_divide_by_zero() -> None:
    from app.cli.audit_shopify_stock import describe

    assert describe([])["diffs"] == 0
    assert describe([])["median_abs"] == 0


async def test_an_untracked_variant_is_reported_with_its_tracking_state() -> None:
    """Shopify reports 0 for a variant it is not tracking. The adapter carries
    the flag rather than the caller having to infer it from a suspicious zero."""
    rows = await _fetch([_page([_variant("A1", 0, tracked=False), _variant("A2", 5)])])
    assert [(r["sku"], r["tracked"]) for r in rows] == [("A1", False), ("A2", True)]

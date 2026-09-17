"""Shopify -> master sync (P2-034) — the replacement for CROSS MALL as the
source of new products.

Two properties are load-bearing and are what this file defends.

**It only adds.** A master Shopify does not return is usually a Rakuten-only
product, not a retired one, so nothing here may archive, deactivate or delete.
The same shape of mistake as the audit's "absent is not zero", one table over.

**A SKU that already has a master gets a mapping, not a second master.** Getting
that wrong would split one product's stock and sales across two rows, and the
duplicate would look exactly like a legitimate new variant.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cli.sync_shopify_masters import Discovery, classify_options, describe, plan

pytestmark = pytest.mark.unit


# --- option classification -----------------------------------------------


def test_colour_and_size_are_read_from_the_option_names() -> None:
    assert classify_options({"色": "gold", "サイズ": "20cm"}) == ("gold", "20cm")
    assert classify_options({"Color": "silver", "Size": "US7"}) == ("silver", "US7")


def test_matching_is_on_the_name_so_values_cannot_be_swapped() -> None:
    """A size whose value reads like a colour must still land in size. Matching
    on values instead would put "gold" in both slots for a two-tone product."""
    assert classify_options({"サイズ": "gold", "色": "22cm"}) == ("22cm", "gold")


def test_an_unrecognised_axis_is_left_blank_rather_than_guessed() -> None:
    """This catalogue has products with a third axis. Forcing it into colour or
    size produces a master whose attributes contradict its own SKU."""
    assert classify_options({"Material": "s925"}) == ("", "")


def test_shopifys_placeholder_option_is_not_a_size() -> None:
    """A product with no real options still reports one, named "Title" with the
    value "Default Title". Recording that as a size would invent a variant."""
    assert classify_options({"Title": "Default Title"}) == ("", "")
    assert classify_options({"サイズ": "Default Title"}) == ("", "")


def test_no_options_at_all() -> None:
    assert classify_options({}) == ("", "")


# --- naming ---------------------------------------------------------------


def test_the_variant_title_is_appended_when_it_adds_something() -> None:
    row = {"product_title": "316L Anchor Necklace", "variant_title": "gold", "sku": "N108gold"}
    assert describe(row) == "316L Anchor Necklace gold"


def test_a_variant_title_already_inside_the_product_title_is_not_repeated() -> None:
    row = {"product_title": "316L Anchor Necklace gold", "variant_title": "gold", "sku": "x"}
    assert describe(row) == "316L Anchor Necklace gold"


def test_the_sku_is_the_last_resort_name() -> None:
    """A nameless product must still register. Falling through to an empty name
    would fail the NOT NULL and take the whole run down with it."""
    assert describe({"product_title": "", "variant_title": "", "sku": "B73"}) == "B73"


# --- adapter parsing ------------------------------------------------------


class _FakeShopify:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

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
    *,
    status: str = "ACTIVE",
    options: list[tuple[str, str]] | None = None,
    product_title: str = "A Product",
    variant_image: str | None = None,
    featured_image: str | None = None,
) -> dict[str, Any]:
    return {
        "sku": sku,
        "title": "v",
        "selectedOptions": [{"name": n, "value": v} for n, v in (options or [])],
        "image": {"url": variant_image} if variant_image else None,
        "product": {
            "id": f"gid://p/{sku}",
            "title": product_title,
            "status": status,
            "featuredImage": {"url": featured_image} if featured_image else None,
        },
    }


async def _fetch(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.adapters.shopify import ShopifyAdapter

    return await ShopifyAdapter.fetch_catalogue(_FakeShopify(pages))  # type: ignore[arg-type]


async def test_draft_and_archived_products_are_not_registered() -> None:
    """They are returned by the Admin API but cannot be sold. Registering them
    puts products in the master that no channel will ever order."""
    rows = await _fetch(
        [
            _page(
                [
                    _variant("LIVE"),
                    _variant("WIP", status="DRAFT"),
                    _variant("OLD", status="ARCHIVED"),
                ]
            )
        ]
    )
    assert [r["sku"] for r in rows] == ["LIVE"]


async def test_a_blank_sku_is_dropped() -> None:
    """It can match no master and cannot become one either — sku_code is the
    unique key."""
    rows = await _fetch([_page([_variant(""), _variant("   "), _variant("B73")])])
    assert [r["sku"] for r in rows] == ["B73"]


async def test_options_arrive_as_a_name_to_value_mapping() -> None:
    variant = _variant("R34silverus5", options=[("色", "silver"), ("サイズ", "US5")])
    rows = await _fetch([_page([variant])])
    assert rows[0]["options"] == {"色": "silver", "サイズ": "US5"}


async def test_the_variant_image_wins_over_the_products_featured_image() -> None:
    rows = await _fetch([_page([_variant("A", variant_image="v.png", featured_image="p.png")])])
    assert rows[0]["image_url"] == "v.png"


async def test_the_featured_image_is_the_fallback() -> None:
    rows = await _fetch([_page([_variant("A", featured_image="p.png")])])
    assert rows[0]["image_url"] == "p.png"


async def test_every_page_is_read() -> None:
    rows = await _fetch([_page([_variant("A")], cursor="c1"), _page([_variant("B")])])
    assert [r["sku"] for r in rows] == ["A", "B"]


# --- planning -------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers `plan`'s reads in the order it makes them: existing Shopify
    mappings, then masters carrying those sku_codes, then the token lookup for
    `related`. Anything beyond that answers empty."""

    def __init__(
        self,
        mapped: list[str],
        masters: dict[str, int],
        related: list[str] | None = None,
    ) -> None:
        self._answers = [
            [(s,) for s in mapped],
            list(masters.items()),
            [(code,) for code in (related or [])],
        ]

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


def _row(sku: str, **kw: Any) -> dict[str, Any]:
    return {
        "sku": sku,
        "variant_title": kw.get("variant_title", ""),
        "product_title": kw.get("product_title", "A Product"),
        "options": kw.get("options", {}),
        "image_url": kw.get("image_url", ""),
    }


async def test_an_already_mapped_variant_is_left_alone() -> None:
    found, summary = await plan(
        _FakeSession(mapped=["N108gold"], masters={}),  # type: ignore[arg-type]
        [_row("N108gold")],
    )
    assert found == []
    assert summary["already_mapped"] == 1
    assert summary["create_master"] == 0


async def test_an_existing_master_gets_a_mapping_not_a_second_master() -> None:
    """The SKU is already a master — it simply was never linked to Shopify.
    Creating another master here would split one product in two."""
    found, summary = await plan(
        _FakeSession(mapped=[], masters={"B34gold22cm": 42}),  # type: ignore[arg-type]
        [_row("B34gold22cm")],
    )
    assert [d.existing_master_id for d in found] == [42]
    assert summary["link_existing_master"] == 1
    assert summary["create_master"] == 0


async def test_a_genuinely_new_variant_is_planned_as_a_new_master() -> None:
    found, summary = await plan(
        _FakeSession(mapped=[], masters={}),  # type: ignore[arg-type]
        [_row("N128silver", product_title="316L Compass Necklace #N128", options={"色": "silver"})],
    )
    assert summary["create_master"] == 1
    assert found == [
        Discovery(
            sku="N128silver",
            name="316L Compass Necklace #N128",
            colour="silver",
            size="",
            token="N128",
            image_url="",
            existing_master_id=None,
        )
    ]


async def test_a_sku_on_two_variants_is_registered_once_and_counted() -> None:
    """Shopify allows it; our sku_code is unique. Planning it twice would fail
    the insert and take the whole run down."""
    found, summary = await plan(
        _FakeSession(mapped=[], masters={}),  # type: ignore[arg-type]
        [_row("N23gold"), _row("N23gold")],
    )
    assert [d.sku for d in found] == ["N23gold"]
    assert summary["duplicate_skus"] == 1


async def test_an_inactive_mapping_is_not_resurrected() -> None:
    """Deactivating a mapping is a decision somebody made. `plan` reads mappings
    without filtering on is_active precisely so that a sync cannot undo it."""
    found, summary = await plan(
        _FakeSession(mapped=["RETIRED"], masters={}),  # type: ignore[arg-type]
        [_row("RETIRED")],
    )
    assert found == []
    assert summary["already_mapped"] == 1


async def test_an_empty_catalogue_plans_nothing() -> None:
    found, summary = await plan(_FakeSession([], {}), [])  # type: ignore[arg-type]
    assert found == []
    assert summary["shopify_variants"] == 0


async def test_a_new_length_is_reported_against_the_master_it_probably_shares_stock_with() -> None:
    """N108gold42 arriving next to an existing N108gold is another length of one
    physical chain, not a new product. Creating it as an independent master with
    its own stock is the wrong repair, so the link has to be visible in the
    report before anyone runs --apply."""
    found, summary = await plan(
        _FakeSession(  # type: ignore[arg-type]
            mapped=[],
            masters={},
            related=["N108gold", "N108silver"],
        ),
        [_row("N108gold42", product_title="316L Anchor Necklace (5mm) #N108")],
    )
    assert found[0].related == ("N108gold", "N108silver")
    assert summary["related_to_existing"] == 1


async def test_a_shorter_token_does_not_claim_a_longer_ones_masters() -> None:
    """ "N10" must not match "N108gold". Without the digit guard every N10 variant
    would be reported as sharing stock with the whole N10x range."""
    found, _ = await plan(
        _FakeSession(  # type: ignore[arg-type]
            mapped=[],
            masters={},
            related=["N108gold", "N10gold"],
        ),
        [_row("N10silver", product_title="Feather Necklace #N10")],
    )
    assert found[0].related == ("N10gold",)


async def test_a_genuinely_new_product_has_no_related_masters() -> None:
    found, summary = await plan(
        _FakeSession(mapped=[], masters={}, related=[]),  # type: ignore[arg-type]
        [_row("N128silver40", product_title="s925 double hook necklace #N128")],
    )
    assert found[0].related == ()
    assert summary["related_to_existing"] == 0

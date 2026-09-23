"""空欄の商品コードの裏にいるもの.

The whole finding is a count: how many DIFFERENT products share the empty key?
That count comes from reading product names back out of `orders.raw_payload`,
and the table holds two different payload shapes — the GraphQL poller stores an
order node, the webhook stores a REST body. A reader that handles one shape
returns "名称不明" for the other half, the distinct-product count comes out far
too low, and the conclusion flips from "this key is dangerous" to "this key is
one product".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.cli.inspect_blank_channel_sku import Product, line_details, report

pytestmark = pytest.mark.unit


def _graphql(line_id: str, name: str, variant: str | None) -> dict:
    node: dict = {"id": f"gid://shopify/LineItem/{line_id}", "name": name, "sku": ""}
    if variant is not None:
        node["variant"] = {"id": f"gid://shopify/ProductVariant/{variant}"}
    return {"lineItems": {"edges": [{"node": node}]}}


def _rest(line_id: str, name: str, variant: str | None) -> dict:
    return {"line_items": [{"id": int(line_id), "name": name, "variant_id": variant}]}


# --- 2つのペイロード形状 ---------------------------------------------------


def test_the_graphql_shape_is_read() -> None:
    payload = _graphql("881", "シルバーリング / FREE", "552")
    assert line_details(payload, "881") == ("シルバーリング / FREE", "552")


def test_the_rest_webhook_shape_is_read() -> None:
    """Orders that arrived by webhook are stored in the other shape entirely."""
    payload = _rest("881", "シルバーリング / FREE", "552")
    assert line_details(payload, "881") == ("シルバーリング / FREE", "552")


def test_the_graphql_id_is_matched_after_stripping_the_gid() -> None:
    """`order_items.line_id` holds the stripped id, the payload holds the gid.
    Comparing them as-is never matches, and every line reads as 名称不明."""
    payload = _graphql("881", "リング", "552")
    assert line_details(payload, "gid://shopify/LineItem/881") == ("", "")
    assert line_details(payload, "881")[0] == "リング"


def test_a_line_that_is_not_in_the_payload_returns_blanks() -> None:
    assert line_details(_graphql("881", "リング", "552"), "999") == ("", "")


def test_a_missing_variant_does_not_break_the_lookup() -> None:
    """A deleted product keeps its line but loses its variant."""
    assert line_details(_graphql("881", "リング", None), "881") == ("リング", "")


def test_a_payload_that_is_not_a_dict_is_tolerated() -> None:
    """`raw_payload` is nullable, and older rows may hold anything."""
    assert line_details(None, "881") == ("", "")
    assert line_details("not a payload", "881") == ("", "")


# --- レポート --------------------------------------------------------------


def _product(name: str, lines: int = 1) -> Product:
    p = Product(name=name)
    for _ in range(lines):
        p.add(
            variant_id=f"v-{name}",
            quantity=1,
            amount=Decimal("3000"),
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
    return p


def test_the_collapse_is_stated_when_several_products_share_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The finding itself. Leaving it to be inferred from a table is how an
    operator resolves the alert and mis-attributes every line behind it."""
    products = {"A": _product("A", 3), "B": _product("B", 2)}
    report(products, [("shopify", "A", 5)])
    out = capsys.readouterr().out

    assert "★" in out
    assert "2種類" in out
    assert "同じ商品に紐づきます" in out


def test_a_single_product_behind_the_key_raises_no_alarm(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One product with no SKU is a data-entry gap, not a collapsed key."""
    report({"A": _product("A", 3)}, [("shopify", "A", 3)])
    out = capsys.readouterr().out

    assert "★" not in out
    assert "同じ商品に紐づきます" not in out


def test_the_alert_section_says_the_name_is_only_the_first_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Otherwise the single 商品名 on the alert reads as the whole story."""
    report({"A": _product("A"), "B": _product("B")}, [("shopify", "A", 2)])
    out = capsys.readouterr().out

    assert "最初に届いた1件" in out


def test_nothing_found_is_said_plainly(capsys: pytest.CaptureFixture[str]) -> None:
    report({}, [])
    assert "該当なし" in capsys.readouterr().out

"""Unit tests for app/cli/build_channel_mapping.py (the variant-level 対応表)."""

from __future__ import annotations

import pytest

from app.cli.build_channel_mapping import (
    build_mapping,
    build_rakuten_index,
    build_shopify_index,
    extract_color,
    extract_size,
    extract_token,
    product_token,
    resolve_rakuten,
    resolve_shopify,
)

# ---------- token / color / size ----------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("316L heart signet ring #R69", "R69"),
        ("シルバー925 ブレスレット B13", "B13"),  # bare token, no '#'
        ("s925 compact horseshoe bracelet #B71", "B71"),  # s925 not a token
        ("メンズ ネックレス #N100", "N100"),  # 3-digit
        ("ジュエリーボックス", None),  # packaging — no token
        ("", None),
        ("#R09 sun ring", "R09"),  # token not at the very end
    ],
)
def test_extract_token(name: str, expected: str | None) -> None:
    assert extract_token(name) == expected


@pytest.mark.unit
def test_extract_color() -> None:
    assert extract_color("??? / GOLD", "B71gold") == "gold"
    assert extract_color("US7 / SILVER") == "silver"
    assert extract_color("Default Title", "R05") == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("US7 / GOLD",), "7"),
        (("USA9",), "9"),
        (("50cm",), "50"),
        (("フリー",), ""),
        (("M",), "M"),
        (("Default Title", "R05"), ""),
    ],
)
def test_extract_size(texts: tuple[str, ...], expected: str) -> None:
    assert extract_size(*texts) == expected


# ---------- Shopify index + resolver ----------


def _shop() -> object:
    rows = [
        ("B13gold", "bracelet #B13", "??? / GOLD"),
        ("B13silver", "bracelet #B13", "??? / SILVER"),
        ("R05", "feather ring #R05", "??? / GOLD"),  # single variant, gold-tagged
        ("R09goldus7", "sun ring #R09", "US7 / GOLD"),
        ("R09goldus9", "sun ring #R09", "US9 / GOLD"),
        ("", "no-sku ring #N90", "??? / GOLD"),  # empty SKU
    ]
    return build_shopify_index(rows)


@pytest.mark.unit
def test_shopify_index_counts_and_empty_sku() -> None:
    idx = _shop()
    assert idx.total == 6
    assert idx.empty_sku == [("N90", "no-sku ring #N90")]


@pytest.mark.unit
def test_resolve_shopify_exact() -> None:
    idx = _shop()
    assert resolve_shopify(idx, "B13", "gold", "") == "B13gold"
    assert resolve_shopify(idx, "R09", "gold", "7") == "R09goldus7"


@pytest.mark.unit
def test_resolve_shopify_single_variant_fallback() -> None:
    # CROSS MALL has no color, Shopify tagged it gold -> still resolves (unique).
    idx = _shop()
    assert resolve_shopify(idx, "R05", "", "") == "R05"


@pytest.mark.unit
def test_resolve_shopify_not_found() -> None:
    idx = _shop()
    assert resolve_shopify(idx, "R07", "gold", "7") == ""  # token absent
    assert resolve_shopify(idx, "N90", "gold", "") == ""  # only empty-sku variant


# ---------- Rakuten index + resolver ----------


def _rk() -> object:
    # Real Rakuten variation values are English lowercase 'gold'/'silver',
    # and color sometimes sits in opt2 with size in opt1 (so scan both).
    rows = [
        {"manage": "030", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""},
        {"manage": "030", "name": "", "sku_mgmt": "030-gold", "opt1": "gold", "opt2": ""},
        {"manage": "030", "name": "", "sku_mgmt": "030-silver", "opt1": "USA7号", "opt2": "silver"},
    ]
    return build_rakuten_index(rows)


@pytest.mark.unit
def test_rakuten_index_token_and_variants() -> None:
    idx = _rk()
    assert idx.token2manage["B13"] == "030"
    assert idx.manage_token["030"] == "B13"
    assert len(idx.var["030"]) == 2


@pytest.mark.unit
def test_resolve_rakuten_by_color() -> None:
    idx = _rk()
    assert resolve_rakuten(idx, "030", "gold", "") == "030-gold"
    assert resolve_rakuten(idx, "030", "silver", "") == "030-silver"
    assert resolve_rakuten(idx, None, "gold", "") == ""


# ---------- end-to-end build_mapping ----------


@pytest.mark.unit
def test_build_mapping_full_and_confirm() -> None:
    shop = _shop()
    rk = build_rakuten_index(
        [
            {"manage": "030c", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""},
            {"manage": "030c", "name": "", "sku_mgmt": "030c-gold", "opt1": "gold", "opt2": ""},
            {"manage": "030c", "name": "", "sku_mgmt": "030c-silver", "opt1": "silver", "opt2": ""},
        ]
    )
    xm_name = {"00037c": "bracelet (no token in crossmall name)", "9999": "ジュエリーボックス"}
    xm_var = {
        "00037c": [
            {"sku": "00037cgold", "color": "gold", "size": ""},
            {"sku": "00037csilver", "color": "silver", "size": ""},
        ],
        "9999": [{"sku": "9999", "color": "", "size": ""}],  # no-token product
    }
    stock_map = {("00037c", "gold", ""): 7, ("00037c", "silver", ""): 3}

    # 00037c is a Rakuten direct code (== manage 030c) whose name carries #B13.
    rk.manage.add("00037c")
    rk.manage_token["00037c"] = "B13"
    rk.var["00037c"] = rk.var["030c"]

    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map=stock_map, rk=rk, shop=shop
    )

    assert stats["full"] == 2  # both colors resolve Shopify + Rakuten
    assert stats["mapping_rows"] == 2
    assert stats["no_token_products"] == 1
    # gold row carries the REAL looked-up Shopify SKU + stock, not the crossmall code.
    gold = next(r for r in mapping if r[2] == "gold")
    assert gold[4] == "B13gold"  # Shopify_SKU
    assert gold[6] == "00037c-gold" or gold[6] == "030c-gold"
    assert gold[7] == 7  # qty
    # the packaging product lands on the confirm sheet.
    assert any("トークン無し" in str(r[-1]) for r in confirm)


@pytest.mark.unit
def test_build_mapping_shopify_only_when_rakuten_missing() -> None:
    shop = _shop()
    rk = build_rakuten_index([])  # no Rakuten data at all
    xm_name = {"x": "ring #R05"}
    xm_var = {"x": [{"sku": "xg", "color": "", "size": ""}]}
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map={}, rk=rk, shop=shop
    )
    assert stats["full"] == 0
    assert stats["shopify_only"] == 1
    assert mapping == []
    assert any("楽天SKU未確定" in str(r[-1]) for r in confirm)


@pytest.mark.unit
def test_product_token_prefers_rakuten_name() -> None:
    rk = build_rakuten_index(
        [{"manage": "00037c", "name": "bracelet #B13", "sku_mgmt": "", "opt1": "", "opt2": ""}]
    )
    # crossmall name has no token, but the matching Rakuten code's name does.
    assert product_token("00037c", {"00037c": "no token here"}, rk) == "B13"

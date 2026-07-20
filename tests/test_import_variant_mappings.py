"""Unit tests for app/cli/import_variant_mappings.py (pure plan builder)."""

from __future__ import annotations

import pytest

from app.cli.import_variant_mappings import (
    MasterSpec,
    build_crossmall_mappings,
    build_plan,
    canonical_sku,
    code_from_crossmall_key,
    crossmall_key,
    dedupe_link_rows,
    dedupe_mapping_rows,
)


@pytest.mark.unit
def test_canonical_sku_prefers_shopify() -> None:
    assert canonical_sku("N21gold", "789") == "N21gold"
    assert canonical_sku("", "789") == "789"  # Rakuten-only fallback
    assert canonical_sku("", "") == ""


def _m(token: str, color: str, size: str, shop: str, rk: str) -> dict[str, str]:
    return {
        "token": token,
        "色": color,
        "サイズ": size,
        "Shopify_SKU": shop,
        "楽天_SKU管理番号": rk,
    }


@pytest.mark.unit
def test_set_bundle_masters_mappings_and_links() -> None:
    mapping = [_m("N23", "gold", "", "N23gold", "501"), _m("N32", "gold", "", "N32gold", "502")]
    bundle = [
        {
            "bundle_token": "N21",
            "色": "gold",
            "親_Shopify_SKU": "N21gold",
            "親_楽天SKU管理番号": "789",
            "構成品トークン(;)": "N23;N32",
        }
    ]
    plan = build_plan(mapping, bundle, [])

    assert set(plan.masters) == {"N23gold", "N32gold", "N21gold"}
    assert plan.masters["N21gold"].is_bundle is True  # the set parent
    assert plan.masters["N23gold"].is_bundle is False  # a component holds stock

    mappings = {(m.channel, m.channel_sku, m.sku_code) for m in plan.mappings}
    assert ("shopify", "N21gold", "N21gold") in mappings
    assert ("rakuten", "789", "N21gold") in mappings
    assert ("rakuten", "501", "N23gold") in mappings

    links = {(link.bundle_sku_code, link.component_sku_code) for link in plan.links}
    assert links == {("N21gold", "N23gold"), ("N21gold", "N32gold")}
    assert plan.warnings == []


@pytest.mark.unit
def test_shared_stock_anklet_master_bracelet_bundle() -> None:
    shared = [
        {
            "token": "B09",
            "色": "gold",
            "主_Shopify_SKU": "B09goldanklet",
            "主_楽天SKU管理番号": "1308",
            "連動_Shopify_SKU": "B09goldbracelet",
            "連動_楽天SKU管理番号": "1307",
        }
    ]
    plan = build_plan([], [], shared)

    assert plan.masters["B09goldanklet"].is_bundle is False  # master stock
    assert plan.masters["B09goldbracelet"].is_bundle is True  # linked, derived

    mappings = {(m.channel, m.channel_sku, m.sku_code) for m in plan.mappings}
    assert ("shopify", "B09goldanklet", "B09goldanklet") in mappings
    assert ("shopify", "B09goldbracelet", "B09goldbracelet") in mappings
    assert ("rakuten", "1307", "B09goldbracelet") in mappings

    links = {(link.bundle_sku_code, link.component_sku_code) for link in plan.links}
    assert links == {("B09goldbracelet", "B09goldanklet")}


@pytest.mark.unit
def test_rakuten_only_row_uses_rakuten_sku_as_code() -> None:
    plan = build_plan([_m("R99", "gold", "", "", "9999")], [], [])
    assert "9999" in plan.masters
    assert plan.masters["9999"].is_bundle is False


@pytest.mark.unit
def test_crossmall_mappings_cover_aliases_and_skip_bundles() -> None:
    masters = {
        "N23gold": MasterSpec("N23gold", "N23 gold", "N23", "gold", "", is_bundle=False),
        "N29gold": MasterSpec(
            "N29gold", "N29 gold", "N29", "gold", "", is_bundle=True
        ),  # set parent
    }
    xm_var = {
        "006c": [{"sku": "x", "color": "gold", "size": ""}],  # alias of N23
        "N23": [{"sku": "y", "color": "gold", "size": ""}],  # alias of N23
        "056c": [{"sku": "z", "color": "gold", "size": ""}],  # -> N29 (bundle) -> skipped
    }
    code2token = {"006c": "N23", "N23": "N23", "056c": "N29"}
    out = build_crossmall_mappings(masters, xm_var, code2token)
    keys = {(m.channel_sku, m.sku_code) for m in out}
    assert ("006c|gold|", "N23gold") in keys  # alias 1 -> master
    assert ("N23|gold|", "N23gold") in keys  # alias 2 -> same master
    assert all(m.sku_code != "N29gold" for m in out)  # bundle parent skipped
    assert all(m.channel == "crossmall" for m in out)


@pytest.mark.unit
def test_crossmall_key_roundtrips_code() -> None:
    assert code_from_crossmall_key(crossmall_key("006c", "gold", "")) == "006c"
    assert code_from_crossmall_key(crossmall_key("027c", "gold", "anklet")) == "027c"
    assert code_from_crossmall_key("H1||") == "H1"


@pytest.mark.unit
def test_dedupe_mapping_rows_collapses_identical_and_flags_conflicts() -> None:
    rows = [
        {"master_sku_id": 1, "channel": "shopify", "channel_sku": "N23gold", "is_active": True},
        # exact duplicate (variant in both mapping + confirm sheet) -> collapsed
        {"master_sku_id": 1, "channel": "shopify", "channel_sku": "N23gold", "is_active": True},
        # same channel SKU, DIFFERENT master -> real ambiguity, keep last + report
        {"master_sku_id": 2, "channel": "rakuten", "channel_sku": "501", "is_active": True},
        {"master_sku_id": 3, "channel": "rakuten", "channel_sku": "501", "is_active": True},
    ]
    deduped, conflicts = dedupe_mapping_rows(rows)
    keys = {(r["channel"], r["channel_sku"]): r["master_sku_id"] for r in deduped}
    assert keys == {("shopify", "N23gold"): 1, ("rakuten", "501"): 3}  # last wins
    assert len(conflicts) == 1
    assert "rakuten:501" in conflicts[0]


@pytest.mark.unit
def test_dedupe_link_rows_collapses_duplicate_bundle_component() -> None:
    rows = [
        {"bundle_master_sku_id": 10, "component_master_sku_id": 20, "quantity_per": 1},
        {"bundle_master_sku_id": 10, "component_master_sku_id": 20, "quantity_per": 2},
        {"bundle_master_sku_id": 10, "component_master_sku_id": 21, "quantity_per": 1},
    ]
    deduped = dedupe_link_rows(rows)
    pairs = {
        (r["bundle_master_sku_id"], r["component_master_sku_id"]): r["quantity_per"]
        for r in deduped
    }
    assert pairs == {(10, 20): 2, (10, 21): 1}  # last wins for the dup pair


@pytest.mark.unit
def test_missing_component_master_warns_not_crashes() -> None:
    bundle = [
        {
            "bundle_token": "N21",
            "色": "gold",
            "親_Shopify_SKU": "N21gold",
            "親_楽天SKU管理番号": "789",
            "構成品トークン(;)": "N23;N32",
        }
    ]
    plan = build_plan([], bundle, [])  # components absent from mapping
    assert plan.links == []  # nothing linked
    assert any("N23" in w for w in plan.warnings)
    assert any("N32" in w for w in plan.warnings)

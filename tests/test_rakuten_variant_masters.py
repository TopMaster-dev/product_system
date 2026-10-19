"""SKU単位の商品を作る計画 — 作ってよいものと、保留すべきものの判定.

Everything this plan decides ends up pointing a live Rakuten page at a master
whose quantity will later be written back to that page. So the tests that matter
are the refusals: a variant the run cannot place must be reported, never guessed.
"""

from __future__ import annotations

import pytest

from app.cli.create_rakuten_variant_masters import (
    ACTION_BLOCKED,
    ACTION_CREATE,
    ACTION_MAP,
    ACTION_SKIP,
    SKU_CODE_MAX,
    build_plan,
    variant_name,
    variant_sku_code,
)
from app.services.rakuten_catalogue import PagePair, RakutenSku, group_variants

pytestmark = pytest.mark.unit

PAIR = [PagePair("073", "073c")]


def sku(manage: str, variant: str, *choices: str, name: str = "リング") -> RakutenSku:
    return RakutenSku(manage, variant, "", 0, name, tuple(choices))


def plan_for(skus, *, proven=None, masters=None, categories=None, mapped=None, pairs=PAIR):
    return build_plan(
        group_variants(skus, pairs),
        proven=proven or {},
        master_by_code=masters or {},
        category_of=categories or {},
        already_mapped=mapped or set(),
    )


# --- 作成 --------------------------------------------------------------------


def test_an_unplaced_variant_becomes_a_new_master_with_both_pages_mapped() -> None:
    """The ordinary case: one master, two mapping rows — one per live page. That
    is what makes the two pages share a single quantity."""
    planned = plan_for([sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")])

    assert len(planned) == 1
    item = planned[0]
    assert item.action == ACTION_CREATE
    assert item.sku_code == "073-19cm-gold"
    assert item.new_mappings == 2


def test_a_new_master_inherits_the_category_of_the_product_it_came_from() -> None:
    """Without this the master is 未分類, and a category-filtered count sheet
    omits it — so the shelf gets counted and the variant does not."""
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm"), sku("073", "1436", "gold", "20cm")],
        proven={("073", "1435"): 7, ("073", "1436"): 7},
        masters={"073c": (7, "073c")},
        categories={7: 42},
        pairs=[],
    )

    assert {item.category_id for item in planned} == {42}
    assert {item.action for item in planned} == {ACTION_CREATE}


def test_the_sku_code_is_stable_across_runs() -> None:
    variant = group_variants([sku("073", "1435", "gold", "19cm")], [])[0]
    assert variant_sku_code(variant) == variant_sku_code(variant)


def test_a_very_long_code_is_hashed_rather_than_truncated() -> None:
    """Two sizes of one product share a long prefix; a bare truncation would
    merge them into one code and therefore one quantity."""
    long_a = sku("P" * 40, "1", "x" * 30, "19cm")
    long_b = sku("P" * 40, "2", "x" * 30, "20cm")
    codes = {variant_sku_code(v) for v in group_variants([long_a, long_b], [])}

    assert len(codes) == 2
    assert all(len(code) <= SKU_CODE_MAX for code in codes)


def test_the_name_carries_the_shop_wording_and_the_choice() -> None:
    variant = group_variants([sku("073", "1435", "gold", "19cm", name="シルバーリング")], [])[0]
    assert variant_name(variant) == "シルバーリング 19cm / gold"


# --- 既存商品への対応づけ ----------------------------------------------------


def test_a_variant_whose_master_already_exists_is_mapped_not_duplicated() -> None:
    """The 28 re-pointable ones: a variant-level master already exists, so this
    creates a mapping and no product."""
    planned = plan_for(
        [sku("b66", "1", "gold", "M"), sku("b66", "2", "silver", "M")],
        proven={("b66", "1"): 7, ("b66", "2"): 7},
        masters={"b66": (7, "B66"), "b66goldm": (8, "B66goldM"), "b66silverm": (9, "B66silverM")},
        pairs=[],
    )

    by_label = {item.variant.label: item for item in planned}
    assert by_label["gold / m"].action == ACTION_MAP
    assert by_label["gold / m"].master_code == "B66goldM"
    assert by_label["m / silver"].master_code == "B66silverM"
    assert all(item.sku_code == "" for item in planned)


def test_a_cleanly_resolved_variant_reuses_its_master() -> None:
    """One variant, one master, claimed by nobody else — already 1:1."""
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        proven={("073", "1435"): 7},
        masters={"b07": (7, "B07")},
    )

    assert planned[0].action == ACTION_MAP
    assert planned[0].master_id == 7
    assert planned[0].new_mappings == 2


def test_an_already_mapped_variant_is_left_alone() -> None:
    """Re-running must not add a second mapping row for the same pair."""
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        mapped={("073", "1435"), ("073c", "931")},
    )

    assert planned[0].action == ACTION_SKIP
    assert planned[0].new_mappings == 0


def test_a_half_mapped_variant_is_still_completed() -> None:
    """One page mapped and the other not is an interrupted run, not a done one."""
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        mapped={("073", "1435")},
    )
    assert planned[0].action == ACTION_CREATE


# --- 保留 (推測しないこと) ---------------------------------------------------


def test_a_variant_whose_skus_name_different_masters_is_held() -> None:
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        proven={("073", "1435"): 7, ("073c", "931"): 9},
    )
    assert planned[0].action == ACTION_BLOCKED
    assert "別々の商品" in planned[0].reason


def test_several_repoint_candidates_are_held_rather_than_chosen() -> None:
    planned = plan_for(
        [sku("b66", "1", "gold", "M"), sku("b66", "2", "silver", "M")],
        proven={("b66", "1"): 7, ("b66", "2"): 7},
        masters={"b66": (7, "B66"), "b66goldm": (8, "B66goldM"), "b66mgold": (9, "B66Mgold")},
        pairs=[],
    )
    gold = next(item for item in planned if "gold" in item.variant.label)
    assert gold.action == ACTION_BLOCKED
    assert "候補が複数" in gold.reason


def test_a_code_that_already_exists_is_held() -> None:
    """sku_code is UNIQUE; colliding would abort the whole transaction."""
    planned = plan_for(
        [sku("073", "1435", "gold", "19cm")],
        masters={"073-19cm-gold": (5, "073-19cm-gold")},
        pairs=[],
    )
    assert planned[0].action == ACTION_BLOCKED
    assert "既に使われています" in planned[0].reason


def test_a_key_too_long_for_rakuten_is_held() -> None:
    """A mapping whose key Rakuten would refuse could never be pushed, so
    writing it would only look like progress."""
    planned = plan_for([sku("P" * 33, "1435", "gold")], pairs=[])
    assert planned[0].action == ACTION_BLOCKED
    assert "32バイト" in planned[0].reason


def test_two_new_variants_cannot_claim_one_generated_code() -> None:
    """Collisions within a single run matter as much as collisions with the
    existing catalogue. `a/b` and `ab` are distinct choices that reduce to one
    code, because the slash cannot survive into a SKU code."""
    planned = plan_for([sku("073", "1", "a/b"), sku("073", "2", "ab")], pairs=[])
    created = [item for item in planned if item.action == ACTION_CREATE]
    held = [item for item in planned if item.action == ACTION_BLOCKED]

    assert len(created) == 1
    assert len(held) == 1
    assert "既に使われています" in held[0].reason

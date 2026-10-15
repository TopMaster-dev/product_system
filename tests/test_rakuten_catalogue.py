"""物理バリアントへのまとめと、商品の割り当て.

This is the logic that decides which quantity reaches which live Rakuten page.
Two failure modes matter and both are silent: pairing that fails splits one
product into two and halves its stock, and pairing that over-matches merges two
real products and writes one number onto both.

The cases below are the real ones measured in the shop's 2026-10-15 export.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.rakuten_catalogue import (
    PagePair,
    RakutenSku,
    VariantAssignment,
    assign_masters,
    group_variants,
    masters_claimed_twice,
    normalise_choice,
    read_catalogue,
    read_pair_table,
    split_candidates,
)

pytestmark = pytest.mark.unit


def sku(manage: str, variant: str, *choices: str, qty: int = 0, merchant: str = "") -> RakutenSku:
    return RakutenSku(manage, variant, merchant, qty, "name", tuple(choices))


# --- 選択肢の正規化 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("mens", "ladies"),
    [
        # 066 — the two pages spell the same size three ways.
        ("US5 (9号～10号)", "USA5（9号～10号の間）"),  # noqa: RUF001 (verbatim from the export)
        # 039 / 040 — one page adds 号.
        ("us7", "us7号"),
        # 066 — spacing around the ampersand.
        ("gold&silver", "gold & silver"),
        # Full-width digits and letters.
        ("US9", "ＵＳ９"),  # noqa: RUF001 (full-width is the point)
    ],
)
def test_the_two_pages_spell_the_same_choice_differently(mens: str, ladies: str) -> None:
    assert normalise_choice(mens) == normalise_choice(ladies)


def test_genuinely_different_choices_stay_different() -> None:
    """Over-matching is the dangerous direction: it merges two real variants."""
    assert normalise_choice("us5") != normalise_choice("us7")
    assert normalise_choice("gold") != normalise_choice("silver")
    assert normalise_choice("19cm") != normalise_choice("20cm")
    # gold & silver is a THIRD product, not either single colour.
    assert normalise_choice("gold&silver") not in {
        normalise_choice("gold"),
        normalise_choice("silver"),
    }


# --- 物理バリアント ----------------------------------------------------------


def test_the_two_pages_of_one_product_become_one_variant() -> None:
    """073 / 073c: six variants over twelve SKUs."""
    skus = [
        sku("073", "1435", "gold", "19cm"),
        sku("073", "1436", "gold", "20cm"),
        sku("073c", "931", "gold", "19cm"),
        sku("073c", "932", "gold", "20cm"),
    ]
    variants = group_variants(skus, [PagePair("073", "073c")])

    assert len(variants) == 2
    for variant in variants:
        assert len(variant.skus) == 2
        assert variant.pages == ("073", "073c")
        assert variant.group == "073"


def test_a_page_listing_colour_and_size_in_the_other_order_still_pairs() -> None:
    pair = group_variants(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "19cm", "gold")],
        [PagePair("073", "073c")],
    )
    assert len(pair) == 1
    assert len(pair[0].skus) == 2


def test_a_size_only_on_one_page_stays_single() -> None:
    """10069: the women's page carries fewer sizes. That is not an error — the
    extra sizes simply have one page and nothing to share with."""
    variants = group_variants(
        [
            sku("10069", "a", "gold", "us5"),
            sku("10069", "b", "gold", "us7"),
            sku("10069c", "x", "gold", "us7"),
        ],
        [PagePair("10069", "10069c")],
    )
    sizes = {v.label: len(v.skus) for v in variants}
    assert sizes == {"gold / us5": 1, "gold / us7": 2}


def test_a_bracketed_specification_is_not_stripped() -> None:
    """10079: the brackets hold the chain width, so gold 3.6mm and gold 5mm are
    two products. Stripping the brackets merged 8 SKUs into 2 and would have
    written one quantity onto four live SKUs."""
    skus = [
        sku("10079", "1295", "gold（3.6mm）"),  # noqa: RUF001 (verbatim from the export)
        sku("10079", "1570", "gold（5mm）"),  # noqa: RUF001 (verbatim from the export)
        sku("10079c", "1299", "gold（3.6mm）"),  # noqa: RUF001 (verbatim from the export)
        sku("10079c", "1572", "gold（5mm）"),  # noqa: RUF001 (verbatim from the export)
    ]
    variants = group_variants(skus, [PagePair("10079", "10079c")])

    assert len(variants) == 2
    assert all(v.kept_brackets for v in variants)
    # Still paired across the two pages — keeping the brackets costs nothing here
    # because both pages spell the specification the same way.
    assert all(v.pages == ("10079", "10079c") for v in variants)
    assert {v.label for v in variants} == {"gold(3.6mm)", "gold(5mm)"}


def test_a_bracketed_size_gloss_is_still_stripped() -> None:
    """066: the brackets restate the US size in Japanese 号 and the two pages
    word it differently, so stripping is the only way they meet."""
    variants = group_variants(
        [
            sku("066", "a", "US5 (9号～10号)"),  # noqa: RUF001 (verbatim from the export)
            sku("066", "b", "US7 (14号～15号)"),  # noqa: RUF001 (verbatim from the export)
            sku("066c", "x", "USA5（9号～10号の間）"),  # noqa: RUF001 (verbatim)
            sku("066c", "y", "USA7（14号～15号の間）"),  # noqa: RUF001 (verbatim)
        ],
        [PagePair("066", "066c")],
    )

    assert len(variants) == 2
    assert not any(v.kept_brackets for v in variants)
    assert all(len(v.skus) == 2 for v in variants)


def test_the_decision_is_per_product_not_global() -> None:
    """One product keeping its brackets must not stop another from stripping —
    the two cases sit side by side in the same export."""
    variants = group_variants(
        [
            sku("10079", "1295", "gold（3.6mm）"),  # noqa: RUF001 (verbatim from the export)
            sku("10079", "1570", "gold（5mm）"),  # noqa: RUF001 (verbatim from the export)
            sku("066", "a", "US5 (9号～10号)"),  # noqa: RUF001 (verbatim from the export)
            sku("066c", "x", "USA5（9号～10号の間）"),  # noqa: RUF001 (verbatim)
        ],
        [PagePair("066", "066c")],
    )
    by_group = {v.group: v for v in variants}

    assert by_group["066"].kept_brackets is False
    assert len(by_group["066"].skus) == 2
    assert len([v for v in variants if v.group == "10079"]) == 2


def test_a_variant_axis_hidden_in_brackets_keeps_every_sku_apart() -> None:
    """202512a: three ring sizes in brackets against three chain lengths. Nine
    SKUs, nine variants — stripping left three."""
    skus = [
        sku("202512a", f"r-sku0000000{i}", f"ring (us{size})", length)
        for i, (size, length) in enumerate(
            [(s, le) for s in (5, 7, 9) for le in ("19cm", "20cm", "22cm")], start=1
        )
    ]
    variants = group_variants(skus, [])

    assert len(variants) == 9
    assert all(len(v.skus) == 1 for v in variants)


def test_no_variant_may_take_two_skus_from_one_page() -> None:
    """The invariant the bracket decision is made against: a page cannot sell one
    physical variant from two SKUs, because the shop could not tell which to
    decrement. Asserted over the real export, not a fixture."""
    csv_path = Path("csv_file/dl-normal-item_20261015101912-1.csv")
    pairs_path = Path("csv_file/phase2/source/楽天ペア対応表-表1.csv")
    if not (csv_path.exists() and pairs_path.exists()):
        pytest.skip("the shop's export is not in this checkout")
    skus, _ = read_catalogue(csv_path)
    pairs, _ = read_pair_table(pairs_path)

    variants = group_variants(skus, pairs)

    doubled = [(v.group, v.label, v.duplicated_pages) for v in variants if v.duplicated_pages]
    assert doubled == []
    assert sum(len(v.skus) for v in variants) == len(skus)


def test_duplicated_pages_names_the_page_that_doubled_up() -> None:
    variant = group_variants([sku("073", "1435", "gold"), sku("073", "1436", "gold")], [])[0]
    assert variant.duplicated_pages == ("073",)


def test_products_not_in_the_pair_table_are_never_merged() -> None:
    """`10060c` and `10060cc` LOOK like a pair under an X/Xc rule, and the
    client's own sheet records 10060cc as a broken duplicate. Only the table
    decides."""
    variants = group_variants(
        [sku("10060c", "1", "gold"), sku("10060cc", "2", "gold")], [PagePair("073", "073c")]
    )
    assert len(variants) == 2
    assert all(len(v.skus) == 1 for v in variants)


def test_the_real_pair_table_loads() -> None:
    path = Path("csv_file/phase2/source/楽天ペア対応表-表1.csv")
    if not path.exists():
        pytest.skip("the client's pair table is not in this checkout")
    pairs, problems = read_pair_table(path)
    assert len(pairs) == 198
    assert problems == []
    assert PagePair("00037", "00037c") in pairs


def test_a_page_used_in_two_pairs_is_refused(tmp_path: Path) -> None:
    """A page can belong to one product only; silently taking the second row
    would attach a variant to the wrong product."""
    path = tmp_path / "pairs.csv"
    path.write_text(
        "No.,商品管理番号(メンズ/無印),商品名(無印側),商品管理番号(レディース/c),商品名(c側)\n"
        "1,073,a,073c,b\n2,073,a,099c,c\n",
        encoding="utf-8-sig",
    )
    pairs, problems = read_pair_table(path)
    assert len(pairs) == 1
    assert any("既に別の組" in p for p in problems)


# --- 商品の割り当て ----------------------------------------------------------


def test_one_proven_sale_maps_both_pages() -> None:
    """The payoff of the pair table: a sale on the men's page maps the women's
    page too, without the women's page ever having sold."""
    variants = group_variants(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        [PagePair("073", "073c")],
    )
    assigned = assign_masters(
        variants, proven={("073", "1435"): 7}, master_by_code={"b07": (7, "B07")}
    )
    assert assigned[0].resolved
    assert assigned[0].master_id == 7
    assert assigned[0].master_code == "B07"


def test_two_pages_proving_different_masters_is_a_conflict_not_a_guess() -> None:
    variants = group_variants(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        [PagePair("073", "073c")],
    )
    assigned = assign_masters(
        variants, proven={("073", "1435"): 7, ("073c", "931"): 9}, master_by_code={}
    )
    assert not assigned[0].resolved
    assert assigned[0].master_id is None
    assert assigned[0].conflicting == (7, 9)


def test_the_shop_code_is_used_only_when_no_sale_proves_otherwise() -> None:
    variants = group_variants([sku("0016", "1392", "gold", merchant="0016goldus7")], [])
    assigned = assign_masters(
        variants, proven={}, master_by_code={"0016goldus7": (3, "0016goldus7")}
    )
    assert assigned[0].master_id == 3
    assert assigned[0].via == "システム連携用SKU番号"


def test_a_variant_we_cannot_place_is_left_unresolved() -> None:
    variants = group_variants([sku("00037", "1483", "gold")], [])
    assigned = assign_masters(variants, proven={}, master_by_code={})
    assert assigned[0].master_id is None
    assert assigned[0].conflicting == ()
    assert not assigned[0].resolved


def test_two_different_variants_claiming_one_master_is_reported() -> None:
    """The defect that would write one quantity onto several live pages."""
    variants = group_variants(
        [sku("073", "1435", "gold", "19cm"), sku("073", "1436", "gold", "20cm")], []
    )
    assigned = assign_masters(
        variants, proven={("073", "1435"): 7, ("073", "1436"): 7}, master_by_code={}
    )
    shared = masters_claimed_twice(assigned)
    assert list(shared) == [7]
    assert len(shared[7]) == 2


def test_page_sharing_inside_one_variant_is_not_double_claiming() -> None:
    """Two SKUs of the SAME variant sharing a master is the feature, not a bug."""
    variants = group_variants(
        [sku("073", "1435", "gold", "19cm"), sku("073c", "931", "gold", "19cm")],
        [PagePair("073", "073c")],
    )
    assigned = assign_masters(variants, proven={("073", "1435"): 7}, master_by_code={})
    assert masters_claimed_twice(assigned) == {}


def test_an_unresolved_variant_never_counts_as_a_claim() -> None:
    unresolved = VariantAssignment(
        group_variants([sku("x", "1")], [])[0], None, "", "", conflicting=()
    )
    assert masters_claimed_twice([unresolved, unresolved]) == {}


# --- 重複している対応づけの切り分け ------------------------------------------


def claim(manage: str, variant: str, *choices: str) -> list[RakutenSku]:
    return [sku(manage, variant, *choices)]


def two_variants_on_one_master(master_id: int = 7) -> list[VariantAssignment]:
    """b66: our master B66 sold both the gold/M and the silver/M SKU, because the
    Rakuten mapping was product-level. Two things on a shelf, one master."""
    variants = group_variants([sku("b66", "1", "gold", "M"), sku("b66", "2", "silver", "M")], [])
    return assign_masters(
        variants,
        proven={("b66", "1"): master_id, ("b66", "2"): master_id},
        master_by_code={"b66": (master_id, "B66")},
    )


def test_a_variant_level_master_already_in_the_catalogue_is_offered() -> None:
    splits = split_candidates(
        two_variants_on_one_master(),
        master_codes=["B66", "B66goldM", "B66silverM", "B66goldS", "B66silverS"],
    )

    assert {s.variant.label: s.candidates for s in splits} == {
        "gold / m": ("B66goldM",),
        "m / silver": ("B66silverM",),
    }
    assert {s.verdict for s in splits} == {"付け替え可能"}


def test_a_hyphenated_master_code_still_matches() -> None:
    splits = split_candidates(
        two_variants_on_one_master(), master_codes=["B66", "B66-gold-M", "B66-silver-M"]
    )
    assert [s.candidates for s in splits] == [("B66-gold-M",), ("B66-silver-M",)]


def test_a_size_letter_inside_a_colour_name_is_not_a_match() -> None:
    """The trap: `s` is a substring of `silver`, so a contains-test would offer
    `B66silverM` to the silver/S variant. Only an exact consumption counts."""
    variants = group_variants([sku("b66", "1", "silver", "S"), sku("b66", "2", "silver", "M")], [])
    assigned = assign_masters(
        variants,
        proven={("b66", "1"): 7, ("b66", "2"): 7},
        master_by_code={"b66": (7, "B66")},
    )

    splits = split_candidates(assigned, master_codes=["B66", "B66silverM"])

    found = {s.variant.label: s.candidates for s in splits}
    assert found == {"m / silver": ("B66silverM",), "s / silver": ()}


def test_no_sibling_master_means_one_has_to_be_created() -> None:
    splits = split_candidates(two_variants_on_one_master(), master_codes=["B66"])
    assert [s.candidates for s in splits] == [(), ()]
    assert {s.verdict for s in splits} == {"商品の新規作成が必要"}


def test_several_candidates_are_reported_rather_than_chosen() -> None:
    """Two masters spelling the same attributes cannot be told apart, and picking
    either would send a live quantity to a guess."""
    splits = split_candidates(
        two_variants_on_one_master(), master_codes=["B66", "B66Mgold", "B66goldM"]
    )
    gold = next(s for s in splits if "gold" in s.variant.label)
    assert gold.candidates == ("B66Mgold", "B66goldM")
    assert gold.verdict == "候補が複数 — 要確認"


def test_a_master_claimed_by_only_one_variant_is_not_a_split() -> None:
    variants = group_variants([sku("b66", "1", "gold", "M")], [])
    assigned = assign_masters(
        variants, proven={("b66", "1"): 7}, master_by_code={"b66": (7, "B66")}
    )
    assert split_candidates(assigned, master_codes=["B66", "B66goldM"]) == []

"""対応づけ案のCSV — 列の数と、切り分けの判定が行に届くこと.

The proposal CSV is what the mapping gets reviewed and applied from, so a row
that has drifted from its header silently shifts every later column — the
reviewer would read a candidate master code under 状態.
"""

from __future__ import annotations

import pytest

from app.cli.inspect_rakuten_variant_map import CSV_HEADER, state_of, variant_rows
from app.services.rakuten_catalogue import (
    RakutenSku,
    assign_masters,
    group_variants,
    split_candidates,
)

pytestmark = pytest.mark.unit

MASTER = 7


def sku(manage: str, variant: str, *choices: str, qty: int = 0) -> RakutenSku:
    return RakutenSku(manage, variant, "", qty, "name", tuple(choices))


def one_master_two_variants() -> tuple[list, list]:
    """b66 sold gold/M and silver/M under one product-level master."""
    variants = group_variants([sku("b66", "1", "gold", "M"), sku("b66", "2", "silver", "M")], [])
    assignments = assign_masters(
        variants,
        proven={("b66", "1"): MASTER, ("b66", "2"): MASTER},
        master_by_code={"b66": (MASTER, "B66")},
    )
    return assignments, split_candidates(assignments, master_codes=["B66", "B66goldM"])


def test_every_row_matches_the_header_width() -> None:
    assignments, splits = one_master_two_variants()
    rows = variant_rows(assignments, {MASTER: 12}, splits)

    assert rows
    assert {len(row) for row in rows} == {len(CSV_HEADER)}


def test_the_split_verdict_and_its_candidate_reach_the_row() -> None:
    assignments, splits = one_master_two_variants()
    rows = variant_rows(assignments, {MASTER: 12}, splits)
    verdict, candidate = CSV_HEADER.index("重複の切り分け"), CSV_HEADER.index("付け替え先の候補")

    by_choice = {row[1]: row for row in rows}
    assert by_choice["gold / m"][verdict] == "付け替え可能"
    assert by_choice["gold / m"][candidate] == "B66goldM"
    # No sibling master spells silver/M, so this one has to be created.
    assert by_choice["m / silver"][verdict] == "商品の新規作成が必要"
    assert by_choice["m / silver"][candidate] == ""


def test_a_variant_in_no_split_leaves_both_columns_empty() -> None:
    """Most variants claim their master alone and need no splitting at all."""
    variants = group_variants([sku("b66", "1", "gold", "M")], [])
    assignments = assign_masters(
        variants, proven={("b66", "1"): MASTER}, master_by_code={"b66": (MASTER, "B66")}
    )
    splits = split_candidates(assignments, master_codes=["B66", "B66goldM"])

    row = variant_rows(assignments, {MASTER: 12}, splits)[0]

    assert splits == []
    assert row[CSV_HEADER.index("重複の切り分け")] == ""
    assert row[CSV_HEADER.index("付け替え先の候補")] == ""


def test_each_page_of_a_shared_variant_gets_its_own_row() -> None:
    """Both live pages must appear, because the push writes to both."""
    from app.services.rakuten_catalogue import PagePair

    variants = group_variants(
        [sku("073", "1435", "gold", "19cm", qty=23), sku("073c", "931", "gold", "19cm", qty=29)],
        [PagePair("073", "073c")],
    )
    assignments = assign_masters(variants, proven={("073", "1435"): MASTER}, master_by_code={})

    rows = variant_rows(assignments, {MASTER: -85}, [])

    sides = {row[CSV_HEADER.index("ページ")]: row for row in rows}
    assert set(sides) == {"メンズ/無印", "レディース/c"}
    assert sides["メンズ/無印"][CSV_HEADER.index("楽天の在庫数")] == 23
    assert sides["レディース/c"][CSV_HEADER.index("楽天の在庫数")] == 29
    # One master, so our figure is repeated — that is the shared stock, and the
    # reviewer sees the two Rakuten pages disagreeing with each other.
    assert {row[CSV_HEADER.index("当システムの在庫数")] for row in rows} == {-85}


def test_a_conflicting_variant_is_marked_for_a_human() -> None:
    variants = group_variants([sku("073", "1435", "gold", "19cm")], [])
    assignments = assign_masters(variants, proven={("073", "1435"): MASTER}, master_by_code={})
    conflicted = type(assignments[0])(assignments[0].variant, None, "", "", conflicting=(7, 9))

    assert state_of(conflicted).startswith("★")
    assert state_of(assignments[0]) == "対応済"

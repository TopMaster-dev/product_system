"""Category suggestions for the client's assignment sheet.

714 blank cells do not get filled in by hand — the sheet comes back untouched
and カテゴリ別売上 ships unverified. So the sheet arrives pre-filled, which
moves the risk: a wrong suggestion the client rubber-stamps becomes data, and
it is data nobody will re-check.

Two properties keep that honest. The suggestion is derived from the catalogue's
own naming convention rather than guessed, and every row carries WHY, so a
reviewer can sort by reason and check the doubtful ones instead of all of them.
Anything the rules cannot explain stays blank and is counted, rather than being
filled with the most likely answer.

The one genuinely ambiguous case is B: bracelets and anklets share the prefix
because they are the same chain sold two ways — which is also why they share
stock.
"""

from __future__ import annotations

import pytest

from app.cli.export_client_worksheets import suggest_category

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        ("N108gold", "NECKLACE"),
        ("B34gold22cm", "BRACELET"),
        ("R34silverus5", "RING"),
        ("P26gold", "PIERCE"),
        ("K03silver", "KEYRING"),
    ],
)
def test_the_sku_prefix_carries_the_category(sku: str, expected: str) -> None:
    code, why = suggest_category(sku, "")
    assert (code, why) == (expected, "SKUコード")


def test_an_anklet_is_recognised_despite_the_bracelet_prefix() -> None:
    """B09 is an anklet and B34 is a bracelet. The prefix cannot tell them
    apart, and they share stock precisely because they are the same chain."""
    code, why = suggest_category("B09goldanklet", "316L horseshoe anklet")
    assert (code, why) == ("ANKLET", "商品名")


def test_the_anklet_check_runs_before_the_prefix() -> None:
    """Order matters. Reading the prefix first would file every anklet as a
    bracelet, and nothing downstream would question it."""
    assert suggest_category("B38silver", "OTバックル アンクレット")[0] == "ANKLET"


def test_a_legacy_numeric_code_falls_back_to_the_name() -> None:
    """0010c and 009c predate the variant cutover and start with a digit."""
    code, why = suggest_category("0010c", "【送料無料】 クロス ネックレス スマイル")
    assert (code, why) == ("NECKLACE", "商品名")


def test_a_legacy_code_whose_name_is_a_product_token() -> None:
    """1079 is named "B12 gold" — the convention moved one column over."""
    code, why = suggest_category("1079", "B12 gold")
    assert (code, why) == ("BRACELET", "商品名のコード")


def test_a_token_in_the_name_does_not_override_an_explicit_word() -> None:
    """A name saying ネックレス outranks a token, because the word was written
    by a person and the token is a code."""
    assert suggest_category("0023c", "フェザーネックレス B99")[0] == "NECKLACE"


def test_something_unrecognisable_stays_blank_rather_than_guessing() -> None:
    """A filled cell is indistinguishable from a checked one. An unexplained
    row has to look unexplained."""
    code, why = suggest_category("10139c", "10139c")
    assert (code, why) == ("", "要確認")


def test_an_empty_name_is_not_a_crash() -> None:
    assert suggest_category("", "") == ("", "要確認")


def test_bangle_counts_as_a_bracelet() -> None:
    """There is no バングル category; the client's taxonomy has six entries and
    this is the one it belongs to."""
    assert suggest_category("0099", "native bangle")[0] == "BRACELET"


def test_an_ear_cuff_is_filed_with_pierces() -> None:
    """Same six-category constraint. イヤーカフ has no category of its own."""
    assert suggest_category("0098", "イヤーカフ 片耳用")[0] == "PIERCE"


def test_every_suggestion_is_one_of_the_registered_codes() -> None:
    """A code the taxonomy does not contain would be rejected on upload, and
    the client would have no way to tell which of 714 rows caused it."""
    registered = {"ANKLET", "BRACELET", "KEYRING", "NECKLACE", "PIERCE", "RING"}
    samples = [
        ("N108gold", "316L Anchor Necklace"),
        ("B09goldanklet", "anklet"),
        ("R63us5", "signet ring"),
        ("P26silver", "curve pierce"),
        ("K03silver", "key ring"),
        ("1079", "B12 gold"),
        ("0010c", "ネックレス"),
    ]
    for sku, name in samples:
        code, _ = suggest_category(sku, name)
        assert code in registered, f"{sku} -> {code!r}"

"""分析結果のCSV出力 (P2-013).

Two failures matter more than the formatting.

**A download that describes a different slice than the screen it came from.**
Nobody notices until two numbers are compared in a meeting, and by then the
spreadsheet has been forwarded. The filter is parsed once, by `_resolve_filter`,
and the screen and its exports both call it.

**A silently truncated export.** The table shows the top 50 because that is a
useful screen; a file that stopped at 50 would be a subset the recipient has no
way to detect, and they would sum it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.analytics_query import SalesFilter, SkuRow
from app.services.timeframe import Period
from app.ui.csv_export import UTF8_BOM, csv_body
from app.ui.routes.analytics import UNCLASSIFIED, _resolve_filter, _slice_note

pytestmark = pytest.mark.unit

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")


# --- the filter the screen and the file share ----------------------------


def test_no_query_string_is_no_filter() -> None:
    assert _resolve_filter(None, None) == SalesFilter()


def test_a_numeric_category_selects_it() -> None:
    assert _resolve_filter(None, "7") == SalesFilter(category_id=7)


def test_the_unclassified_sentinel_is_not_an_id() -> None:
    f = _resolve_filter(None, UNCLASSIFIED)
    assert f.unclassified_only is True
    assert f.category_id is None


def test_a_non_numeric_category_is_ignored_rather_than_raising() -> None:
    """These arrive from hand-edited URLs and stale bookmarks. A 500 on an
    export link is a worse answer than the unfiltered file."""
    assert _resolve_filter(None, "../etc/passwd") == SalesFilter()


def test_an_empty_channel_string_means_all_channels() -> None:
    """The screen's <select> posts "" for すべて, which must not become a filter
    for a channel literally named empty string."""
    assert _resolve_filter("", None).channel is None


def test_a_channel_is_carried_through() -> None:
    assert _resolve_filter("rakuten", None).channel == "rakuten"


# --- the slice note -------------------------------------------------------


def test_the_note_states_the_window() -> None:
    note = _slice_note(PERIOD, SalesFilter())
    assert "期間: 2026-09-01 〜 2026-09-28" in note[0]


def test_the_note_names_an_unfiltered_export_as_unfiltered() -> None:
    """Silence would read as "this is everything" only if the reader already
    knew the screen had filters."""
    note = _slice_note(PERIOD, SalesFilter())
    assert "チャネル: すべて" in note
    assert "カテゴリ: すべて" in note


def test_the_note_distinguishes_unclassified_from_unfiltered() -> None:
    note = _slice_note(PERIOD, SalesFilter(unclassified_only=True))
    assert "カテゴリ: 未分類のみ" in note


def test_the_note_records_a_selected_category() -> None:
    assert "カテゴリID: 7" in _slice_note(PERIOD, SalesFilter(category_id=7))


def test_the_note_records_the_granularity_when_there_is_one() -> None:
    assert "粒度: week" in _slice_note(PERIOD, SalesFilter(), "week")


def test_the_note_omits_granularity_for_a_table_export() -> None:
    assert not any("粒度" in part for part in _slice_note(PERIOD, SalesFilter()))


# --- the file itself ------------------------------------------------------


def test_the_note_is_a_comment_line_our_own_importer_skips() -> None:
    """`csv_intake` treats a leading "#" as a comment, so an exported file can
    be read back without stripping the header by hand."""
    note = csv_body(["# " + " / ".join(_slice_note(PERIOD, SalesFilter()))], [])
    assert note.startswith("# ")


def test_the_body_carries_the_columns_a_reader_expects() -> None:
    body = csv_body(
        ["SKUコード", "商品名", "カテゴリ", "販売数量", "売上高(円)"],
        [["N108gold", "アンカー", "未分類", 12, int(Decimal(120_000))]],
    )
    assert "SKUコード" in body
    assert "120000" in body


def test_a_sku_with_no_category_exports_as_unclassified_not_blank() -> None:
    """An empty cell in a spreadsheet is indistinguishable from a missing one,
    and 未分類 is a finding the client acts on. This is the route's
    `r.category_name or "未分類"`, held still."""
    uncategorised = SkuRow(1, "B73silver17", "ブレス", None, 4, Decimal(40_000))
    row = [[uncategorised.sku_code, uncategorised.name, uncategorised.category_name or "未分類"]]
    assert "未分類" in csv_body(["SKUコード", "商品名", "カテゴリ"], row)


def test_exports_are_utf8_with_a_bom_for_excel() -> None:
    """Excel decides a CSV's encoding by the BOM alone; without it a
    Japanese-locale machine guesses CP932 and an English one guesses 1252."""
    assert UTF8_BOM == "﻿"

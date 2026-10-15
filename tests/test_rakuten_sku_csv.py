"""楽天の商品データ (dl-normal-item) の読み取り.

The export is the whole SKU-level mapping in one file, and it is the input the
stock push will be keyed on. A quiet misread here would push one SKU's stock onto
another, so the shapes that actually occur in the real file are pinned: product
header rows with no SKU, SKU管理番号 reused across products, blank
システム連携用SKU番号, and 在庫数 that is empty or negative.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.cli.inspect_rakuten_sku_csv import Coverage, Match, print_ambiguity
from app.services.rakuten_catalogue import (
    COL_ITEM_NAME,
    COL_MANAGE_NUMBER,
    COL_MERCHANT_SKU,
    COL_QUANTITY,
    COL_VARIANT_ID,
    RakutenSku,
    read_catalogue,
)

pytestmark = pytest.mark.unit

HEADERS = [
    COL_MANAGE_NUMBER,
    "商品番号",
    COL_ITEM_NAME,
    COL_VARIANT_ID,
    COL_MERCHANT_SKU,
    COL_QUANTITY,
]


def _write(tmp_path: Path, rows: list[list[str]], *, encoding: str = "cp932") -> Path:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADERS)
    writer.writerows(rows)
    path = tmp_path / "dl-normal-item.csv"
    path.write_text(buf.getvalue(), encoding=encoding)
    return path


def test_product_header_rows_are_not_skus(tmp_path: Path) -> None:
    """The real export carries 423 rows that describe the PRODUCT and leave
    SKU管理番号 empty. Counting them as SKUs would invent stock records."""
    path = _write(
        tmp_path,
        [
            ["10014", "10014", "Anchor Necklace", "", "", ""],
            ["10014", "10014", "Anchor Necklace", "1542", "10014gold", "7"],
            ["10014", "10014", "Anchor Necklace", "1543", "10014silver", "3"],
        ],
    )
    skus, problems = read_catalogue(path)
    assert [s.variant_id for s in skus] == ["1542", "1543"]
    assert problems == []


def test_the_same_sku_id_under_two_products_stays_two_rows(tmp_path: Path) -> None:
    """132 SKU管理番号 are reused across products in the real file. Keyed on the
    SKU alone they would collapse into one and the push would target the wrong
    product; the pair keeps them apart."""
    path = _write(
        tmp_path,
        [
            ["10014", "", "A", "gold", "10014gold", "7"],
            ["10050c", "", "B", "gold", "10050cgold", "4"],
        ],
    )
    skus, _ = read_catalogue(path)
    assert len(skus) == 2
    assert {(s.manage_number, s.variant_id) for s in skus} == {
        ("10014", "gold"),
        ("10050c", "gold"),
    }


def test_a_genuinely_duplicated_pair_is_reported_not_silently_kept(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            ["10014", "", "A", "1542", "x", "7"],
            ["10014", "", "A", "1542", "x", "9"],
        ],
    )
    skus, problems = read_catalogue(path)
    assert len(skus) == 1
    assert any("重複" in p for p in problems)


def test_missing_quantity_reads_as_zero_not_as_a_crash(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            ["10014", "", "A", "1542", "x", ""],
            ["10014", "", "A", "1543", "y", "-2"],
            ["10014", "", "A", "1544", "z", "  11  "],
        ],
    )
    skus, _ = read_catalogue(path)
    assert [s.quantity for s in skus] == [0, -2, 11]


def test_a_sku_with_no_merchant_code_is_still_a_sku(tmp_path: Path) -> None:
    """Only 755 of 1,107 rows carry システム連携用SKU番号; the rest still have
    stock and still need pushing."""
    path = _write(tmp_path, [["073", "", "Ring", "1438", "", "5"]])
    skus, _ = read_catalogue(path)
    assert skus[0].merchant_sku_id == ""
    assert skus[0].quantity == 5


def test_a_sku_without_a_product_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, [["", "", "Orphan", "1542", "x", "1"]])
    skus, problems = read_catalogue(path)
    assert skus == []
    assert any("商品管理番号が空" in p for p in problems)


def test_a_file_saved_from_excel_as_utf8_still_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, [["10014", "", "A", "1542", "x", "7"]], encoding="utf-8-sig")
    skus, problems = read_catalogue(path)
    assert [s.variant_id for s in skus] == ["1542"]
    assert problems == []


def test_a_file_missing_the_sku_column_says_so(tmp_path: Path) -> None:
    path = tmp_path / "wrong.csv"
    path.write_text(f"{COL_MANAGE_NUMBER},{COL_ITEM_NAME}\n10014,A\n", encoding="cp932")
    skus, problems = read_catalogue(path)
    assert skus == []
    assert any(COL_VARIANT_ID in p for p in problems)


def test_a_placeholder_sku_is_flagged_but_usable() -> None:
    """95 SKUs are still RMS's auto-generated r-sku… . The pair is unique, so the
    push works; docs/28 told the client they need not rename them."""
    auto = RakutenSku("10014", "r-sku00000001", "", 3, "A")
    named = RakutenSku("10014", "gold-s-B50", "10087goldS", 3, "A")
    assert auto.is_placeholder
    assert not named.is_placeholder


@pytest.mark.skipif(
    not Path("csv_file/dl-normal-item_20261015101912-1.csv").exists(),
    reason="the client's export is not in this checkout",
)
def test_the_real_export_parses_to_the_figures_the_client_quoted() -> None:
    """馬渡様 said 419 商品 / 1,107 SKU on 2026-10-15. If a future export parses
    to something else, the reader changed or the file did."""
    skus, problems = read_catalogue(Path("csv_file/dl-normal-item_20261015101912-1.csv"))
    assert len({s.manage_number for s in skus}) == 419
    assert len(skus) == 1107
    assert problems == []
    # Every pair unique — the precondition for keying the push on it.
    assert len({(s.manage_number, s.variant_id) for s in skus}) == 1107


# --- 同じ商品を指すSKUの検出 -------------------------------------------------


def _match(manage: str, variant: str, master_id: int, code: str = "M") -> Match:
    return Match(
        sku=RakutenSku(manage, variant, "", 0, "n"),
        master_id=master_id,
        master_code=code,
        via="受注実績",
    )


def test_skus_sharing_one_master_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    """Four SKUs of one product pointing at one master means our quantity would
    be written to all four. That must be seen before anything is pushed."""
    coverage = Coverage(
        matches=[
            _match("10090", "goldus7", 1),
            _match("10090", "goldus9", 1),
            _match("10090", "silverus7", 1),
            _match("0020", "1399", 2, "B20"),
        ]
    )
    print_ambiguity(coverage)
    out = capsys.readouterr().out
    assert "3件の楽天SKU" in out
    assert "10090/goldus7" in out
    assert "0020/1399" not in out  # the unambiguous one is not an issue


def test_a_one_to_one_mapping_reports_nothing_to_fix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    coverage = Coverage(matches=[_match("0020", "1399", 2), _match("0021", "1400", 3)])
    print_ambiguity(coverage)
    assert "1対1" in capsys.readouterr().out


def test_unmatched_skus_are_not_counted_as_sharing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`master_id is None` on two rows is not two rows sharing a master."""
    unmatched = [Match(sku=RakutenSku("00037", v, "", 0, "n")) for v in ("1483", "1484")]
    print_ambiguity(Coverage(matches=unmatched))
    assert "1対1" in capsys.readouterr().out

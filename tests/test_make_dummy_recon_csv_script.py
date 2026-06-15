"""Unit tests for scripts/make_dummy_recon_csv.py."""

from __future__ import annotations

import csv as _csv
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from make_dummy_recon_csv import (  # noqa: E402
    CSV_ENCODING,
    CSV_HEADER,
    SkuRow,
    main,
    parse_sku_arg,
    write_csv,
)

# ---------- parse_sku_arg ----------


@pytest.mark.unit
def test_parse_sku_arg_positive_qty() -> None:
    assert parse_sku_arg("00037c:231") == SkuRow(code="00037c", qty=231)


@pytest.mark.unit
def test_parse_sku_arg_negative_qty_preserved() -> None:
    assert parse_sku_arg("0010c:-69") == SkuRow(code="0010c", qty=-69)


@pytest.mark.unit
def test_parse_sku_arg_zero_qty() -> None:
    assert parse_sku_arg("NEW:0") == SkuRow(code="NEW", qty=0)


@pytest.mark.unit
def test_parse_sku_arg_trims_whitespace() -> None:
    assert parse_sku_arg("  ABC : 12  ") == SkuRow(code="ABC", qty=12)


@pytest.mark.unit
def test_parse_sku_arg_rejects_missing_colon() -> None:
    with pytest.raises(ValueError, match="CODE:QTY"):
        parse_sku_arg("00037c")


@pytest.mark.unit
def test_parse_sku_arg_rejects_missing_code() -> None:
    with pytest.raises(ValueError, match="missing CODE"):
        parse_sku_arg(":12")


@pytest.mark.unit
def test_parse_sku_arg_rejects_non_int_qty() -> None:
    with pytest.raises(ValueError, match="integer"):
        parse_sku_arg("X:abc")


# ---------- write_csv ----------


@pytest.mark.unit
def test_write_csv_uses_cp932_encoding_and_header(tmp_path: Path) -> None:
    out = tmp_path / "dummy.csv"
    write_csv([SkuRow("00037c", 231), SkuRow("0010c", -69)], out)
    # Round-trip via CP932 to confirm encoding
    with out.open("r", encoding=CSV_ENCODING, newline="") as f:
        reader = _csv.reader(f)
        rows = list(reader)
    assert rows[0] == CSV_HEADER
    assert rows[1] == ["u", "00037c", "", "", "231"]
    assert rows[2] == ["u", "0010c", "", "", "-69"]


@pytest.mark.unit
def test_write_csv_creates_missing_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "subdir" / "dummy.csv"
    write_csv([SkuRow("A", 1)], out)
    assert out.exists()


@pytest.mark.unit
def test_write_csv_overwrites_existing(tmp_path: Path) -> None:
    out = tmp_path / "dummy.csv"
    write_csv([SkuRow("OLD", 999)], out)
    write_csv([SkuRow("NEW", 1)], out)
    with out.open("r", encoding=CSV_ENCODING, newline="") as f:
        rows = list(_csv.reader(f))
    assert len(rows) == 2  # header + 1 row
    assert rows[1][1] == "NEW"


@pytest.mark.unit
def test_csv_is_compatible_with_aggregate_csv_by_product(tmp_path: Path) -> None:
    """The output must be readable by app.cli.reconcile_inventory's
    aggregator without any further transformation."""
    out = tmp_path / "dummy.csv"
    write_csv(
        [
            SkuRow("ABC", 10),
            SkuRow("ABC", 5),  # CROSS MALL emits multiple rows per code
            SkuRow("XYZ", 7),
        ],
        out,
    )
    from app.cli.reconcile_inventory import aggregate_csv_by_product

    agg = aggregate_csv_by_product(out)
    assert agg == {"ABC": 15, "XYZ": 7}


# ---------- main() ----------


@pytest.mark.unit
def test_main_writes_csv_to_disk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "x.csv"
    code = main(["--out", str(out), "--sku", "A:1", "--sku", "B:2"])
    assert code == 0
    assert "wrote 2 rows" in capsys.readouterr().out
    assert out.exists()


@pytest.mark.unit
def test_main_usage_error_on_missing_required_flag() -> None:
    saved = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main(["--out", "x.csv"])  # missing --sku
    finally:
        sys.stderr = saved
    assert code == 2


@pytest.mark.unit
def test_main_usage_error_on_malformed_sku() -> None:
    saved = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main(["--out", "x.csv", "--sku", "no-colon"])
    finally:
        sys.stderr = saved
    assert code == 2

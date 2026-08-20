"""Unit tests for the shared CSV-intake helper."""

from __future__ import annotations

import pytest

from app.ui.csv_intake import (
    ColumnSpec,
    CsvDecodeError,
    CsvSpec,
    decode_csv,
    inspect,
    int_validator,
    iter_rows,
    resolve_header,
)

pytestmark = pytest.mark.unit

SPEC = CsvSpec(
    columns=(
        ColumnSpec(canonical="sku_code", aliases=("SKUコード", "SKU")),
        ColumnSpec(
            canonical="実数",
            aliases=("在庫数", "quantity"),
            validator=int_validator("実数が数値ではありません: '{value}'"),
        ),
    )
)


def _csv(text: str, encoding: str = "utf-8-sig") -> bytes:
    return text.encode(encoding)


def test_decodes_utf8_bom_and_cp932() -> None:
    assert decode_csv(_csv("あ"), SPEC) == "あ"
    assert decode_csv("あ".encode("cp932"), SPEC) == "あ"


def test_undecodable_bytes_raise() -> None:
    with pytest.raises(CsvDecodeError):
        decode_csv(bytes([0x81, 0x20, 0x81, 0x20]), CsvSpec(columns=(), encodings=("cp932",)))


def test_header_aliases_resolve() -> None:
    index, missing = resolve_header(["SKUコード", "在庫数"], SPEC)
    assert index == {"sku_code": 0, "実数": 1}
    assert missing == []


def test_missing_required_column_is_fatal() -> None:
    result = inspect(_csv("sku_code\nN23gold\n"), SPEC)
    assert result.fatal and "実数" in result.fatal[0]
    assert result.valid_rows == 0


def test_row_issues_carry_the_excel_line_number() -> None:
    result = inspect(_csv("sku_code,実数\nN23gold,27\n,5\nB09,abc\n"), SPEC)
    assert result.fatal == []
    assert result.valid_rows == 1
    assert result.total_rows == 3
    reasons = {i["line"]: i["reason"] for i in result.row_issues}
    assert reasons[3] == "sku_codeが空です"
    assert reasons[4] == "実数が数値ではありません: 'abc'"


def test_allow_empty_skips_the_row_without_reporting_it() -> None:
    """A column that must EXIST but may be blank on a row (the CROSS MALL
    stock export writes such rows) is not an operator error."""
    spec = CsvSpec(
        columns=(
            ColumnSpec(canonical="sku_code"),
            ColumnSpec(canonical="実数", allow_empty=True),
        )
    )
    result = inspect(_csv("sku_code,実数\nN23gold,\nB09,3\n"), spec)
    assert result.row_issues == []
    assert result.valid_rows == 1


def test_required_column_presence_is_separate_from_empty_values() -> None:
    """allow_empty must NOT make the column itself optional."""
    spec = CsvSpec(columns=(ColumnSpec(canonical="実数", allow_empty=True),))
    result = inspect(_csv("別の列\n1\n"), spec)
    assert result.fatal and "実数" in result.fatal[0]


def test_row_issue_cap_prevents_flooding() -> None:
    body = "\n".join(",5" for _ in range(200))
    spec = CsvSpec(columns=SPEC.columns, max_row_issues=10)
    result = inspect(_csv(f"sku_code,実数\n{body}\n"), spec)
    assert len(result.row_issues) == 10
    assert result.total_rows == 200


def test_iter_rows_yields_only_valid_rows_keyed_canonically() -> None:
    rows = list(iter_rows(_csv("SKUコード,在庫数\nN23gold,27\n,9\nB09,abc\nB12,3\n"), SPEC))
    assert rows == [{"sku_code": "N23gold", "実数": "27"}, {"sku_code": "B12", "実数": "3"}]


def test_empty_file_is_fatal() -> None:
    assert inspect(b"", SPEC).fatal


def test_header_only_file_has_no_rows() -> None:
    result = inspect(_csv("sku_code,実数\n"), SPEC)
    assert result.fatal == []
    assert result.total_rows == 0


def test_leading_comment_lines_are_skipped() -> None:
    """Every template we hand the client opens with `#` guidance lines, and the
    client edits and returns THAT file. Treating line 1 as the header rejected
    the upload with "必須列がありません" — an error whose real fix ("delete the
    instructions") the message gave no way to guess."""
    result = inspect(
        _csv("# これは説明行です\n# kind の選択肢: packaging / coupon\nsku_code,実数\nH1,3\n"),
        SPEC,
    )
    assert result.fatal == []
    assert result.valid_rows == 1


def test_line_numbers_still_point_at_the_excel_row() -> None:
    """Skipping comments must not shift the reported line number — an operator
    fixes the file by line number, so an off-by-two sends them to the wrong row."""
    result = inspect(
        _csv("# 説明1\n# 説明2\nsku_code,実数\nOK,1\n,5\nB09,abc\n"),
        SPEC,
    )
    reasons = {i["line"]: i["reason"] for i in result.row_issues}
    assert reasons[5] == "sku_codeが空です"  # physical line 5 in the file
    assert reasons[6] == "実数が数値ではありません: 'abc'"


def test_a_hash_inside_data_is_not_a_comment() -> None:
    """Only rows BEFORE the header are guidance; after it, `#` is content."""
    spec = CsvSpec(columns=(ColumnSpec(canonical="備考"), ColumnSpec(canonical="sku_code")))
    rows = list(iter_rows(_csv("備考,sku_code\n#1 人気,N23gold\n"), spec))
    assert rows == [{"備考": "#1 人気", "sku_code": "N23gold"}]


def test_comment_skipping_can_be_disabled() -> None:
    spec = CsvSpec(columns=SPEC.columns, comment_prefix="")
    assert inspect(_csv("# not a comment\nsku_code,実数\nH1,3\n"), spec).fatal


def test_file_of_only_comments_reports_no_header() -> None:
    assert inspect(_csv("# just guidance\n# and more\n"), SPEC).fatal

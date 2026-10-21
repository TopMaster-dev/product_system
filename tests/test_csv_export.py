"""Unit tests for the CSV download helper.

Excel decides a CSV's encoding by one rule: a UTF-8 BOM means UTF-8, and its
absence means the system ANSI codepage. It never sees the HTTP charset, because
by the time the file is on disk there is no HTTP header left.

Both exports handed to a human on 2026-08-29 got this wrong in opposite
directions — one UTF-8 without a BOM, one CP932 without a BOM — so these tests
pin the rule rather than any one call site.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.csv_export import UTF8_BOM, csv_body, csv_response, write_csv

pytestmark = pytest.mark.unit


def _payload(response) -> bytes:  # type: ignore[no-untyped-def]
    return bytes(response.body)


def test_body_starts_with_the_bom() -> None:
    """Without it Excel guesses, and on a non-Japanese Windows it guesses wrong."""
    body = _payload(csv_response("区分,SKU管理番号\n仮値,r-sku001\n", filename="x.csv"))
    assert body.startswith(b"\xef\xbb\xbf")


def test_japanese_survives_a_round_trip_through_utf8() -> None:
    text = _payload(csv_response("区分\n仮値\n", filename="x.csv")).decode("utf-8-sig")
    assert text.startswith("区分")
    assert "仮値" in text


def test_the_old_cp932_bytes_are_no_longer_emitted() -> None:
    """仮値 in CP932 is 89 BC 92 6C, which an English-locale Windows renders as
    `‰¼'l` — the exact mojibake reported from production."""
    body = _payload(csv_response("仮値\n", filename="x.csv"))
    assert b"\x89\xbc\x92\x6c" not in body
    assert "仮値".encode() in body


def test_charset_is_declared_utf8() -> None:
    response = csv_response("a,b\n", filename="x.csv")
    assert "charset=utf-8" in response.media_type
    assert response.headers["Content-Disposition"] == 'attachment; filename="x.csv"'


def test_csv_body_quotes_and_escapes_like_the_stdlib() -> None:
    body = csv_body(["a", "b"], [["x,y", 'he said "hi"']])
    parsed = list(csv.reader(io.StringIO(body)))
    assert parsed == [["a", "b"], ["x,y", 'he said "hi"']]


def test_body_carries_no_bom_of_its_own() -> None:
    """Only `csv_response` adds it — otherwise a body passed through both would
    carry two, and the second BOM would show up as a literal cell value."""
    assert not csv_body(["区分"], []).startswith(UTF8_BOM)


@pytest.mark.unit
def test_write_csv_does_not_double_the_carriage_return(tmp_path: Path) -> None:
    """`csv.writer` ends each record with CRLF already. Writing that without
    `newline=""` translates the LF again, so every line lands as CR CR LF and
    Excel shows a blank row between every row of data. Measured on the count
    sheets 2026-10-21: 61 of 61 lines doubled, and they were about to be sent to
    the client as a 60-item sheet padded with 60 empty rows."""
    path = write_csv(tmp_path / "sheet.csv", csv_body(["a", "b"], [[1, 2], [3, 4]]))
    raw = path.read_bytes()

    assert b"\r\r\n" not in raw
    assert raw.count(b"\r\n") == 3  # header + two rows
    assert raw.startswith(UTF8_BOM.encode("utf-8"))


@pytest.mark.unit
def test_write_csv_round_trips_through_a_reader(tmp_path: Path) -> None:
    """The practical check: no blank rows when something reads it back."""
    path = write_csv(tmp_path / "sheet.csv", csv_body(["sku_code", "数量"], [["073", 5]]))
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))

    assert rows == [["sku_code", "数量"], ["073", "5"]]


@pytest.mark.unit
def test_write_csv_creates_the_directory(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "deep" / "er" / "sheet.csv", csv_body(["a"], [["x"]]))
    assert path.exists()

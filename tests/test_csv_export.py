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

import pytest

from app.ui.csv_export import UTF8_BOM, csv_body, csv_response

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

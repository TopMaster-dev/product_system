"""Unit tests for the pure helpers behind the new admin UI screens:
CSV inspection (在庫CSV取込) and error localization (同期エラー)."""

from __future__ import annotations

import pytest

from app.ui.routes.reconcile import inspect_csv
from app.ui.routes.sync_errors import localize_error

pytestmark = pytest.mark.unit


def _cp932(text: str) -> bytes:
    return text.encode("cp932")


def test_inspect_csv_accepts_valid_file() -> None:
    data = _cp932("商品コード,在庫数量\r\n006c,27\r\nH1,3\r\n")
    result = inspect_csv(data)
    assert result["fatal"] == []
    assert result["valid_rows"] == 2
    assert result["total_rows"] == 2
    assert result["row_issues"] == []


def test_inspect_csv_flags_missing_required_column() -> None:
    data = _cp932("商品コード\r\n006c\r\n")
    result = inspect_csv(data)
    assert result["fatal"]
    assert any("在庫数量" in e for e in result["fatal"])


def test_inspect_csv_rejects_non_cp932_encoding() -> None:
    # 0x81 is a lead byte; 0x20 is an invalid trail byte -> CP932 decode raises.
    result = inspect_csv(bytes([0x81, 0x20, 0x81, 0x20]))
    assert result["fatal"]
    assert any("Shift-JIS" in e for e in result["fatal"])


def test_inspect_csv_reports_row_level_issues() -> None:
    data = _cp932("商品コード,在庫数量\r\n006c,abc\r\n,5\r\nH1,3\r\n")
    result = inspect_csv(data)
    assert result["fatal"] == []
    assert result["valid_rows"] == 1  # only H1,3 is valid
    reasons = {i["line"]: i["reason"] for i in result["row_issues"]}
    assert 2 in reasons  # non-numeric qty
    assert 3 in reasons  # empty 商品コード


def test_inspect_csv_empty_file_is_fatal() -> None:
    assert inspect_csv(b"")["fatal"]


def test_localize_error_timeout() -> None:
    assert "タイムアウト" in localize_error("ReadTimeout", "read timed out")


def test_localize_error_rate_limit_from_status() -> None:
    assert "レート制限" in localize_error("HTTPStatusError", "429 Too Many Requests")


def test_localize_error_auth_from_status() -> None:
    assert "認証" in localize_error("HTTPStatusError", "401 Unauthorized")


def test_localize_error_falls_back_to_generic() -> None:
    msg = localize_error("SomethingUnmapped", "boom")
    assert msg
    assert "同期" in msg

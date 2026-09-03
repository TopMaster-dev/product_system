"""The worksheet we hand the client to fill in.

Two things can go wrong here and neither shows up locally. The SQL is Postgres
-only and runs only against production, so a broken statement is discovered by
running it on prod. And the file is opened in Excel on a Windows desktop, where
a missing BOM silently mangles every Japanese product name — the failure that
already reached this client once.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.cli.export_unmapped_worksheet import (
    HEADER,
    WorksheetRow,
    collect_rows,
    write_worksheet,
)

pytestmark = pytest.mark.unit


class _CompilingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(
            str(
                statement.compile(  # type: ignore[attr-defined]
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return type("_Empty", (), {"all": lambda self: []})()


async def _sql(*, include_mapped_keys: bool) -> str:
    session = _CompilingSession()
    await collect_rows(session, include_mapped_keys=include_mapped_keys)  # type: ignore[arg-type]
    return " ".join(session.statements)


async def test_the_query_compiles_and_is_scoped_to_rakuten() -> None:
    """`channel` lives on orders; without the join this reports Shopify's
    unmapped lines as Rakuten's."""
    sql = await _sql(include_mapped_keys=False)
    assert "JOIN orders" in sql
    assert "orders.channel = 'rakuten'" in sql
    assert "order_items.master_sku_id IS NULL" in sql


async def test_keys_that_already_have_a_mapping_are_excluded_by_default() -> None:
    """Those need a remap from us, not an answer from the client. Asking about
    them wastes their time and invites a confusing reply."""
    assert "NOT IN" in (await _sql(include_mapped_keys=False)).upper()


async def test_all_includes_them_for_our_own_review() -> None:
    assert "NOT IN" not in (await _sql(include_mapped_keys=True)).upper()


def _row(**overrides: object) -> WorksheetRow:
    base = {
        "manage_number": "10001",
        "product_name": "馬蹄 ネックレス",
        "lines": 3,
        "quantity": 5,
        "amount": 12000,
        "first_date": "2025-04-01",
        "last_date": "2026-08-30",
    }
    return WorksheetRow(**{**base, **overrides})  # type: ignore[arg-type]


def test_a_row_fills_every_column() -> None:
    """A short row shifts every value left of it under a wrong header."""
    assert len(_row().as_csv()) == len(HEADER)


def test_the_two_answer_columns_are_left_empty() -> None:
    row = _row().as_csv()
    assert row[-2:] == ("", ""), "商品コード and 備考 are the client's to fill"


def test_the_file_carries_a_bom_and_reads_back_intact(tmp_path) -> None:
    """Without the BOM Excel falls back to the ANSI codepage and every Japanese
    name arrives mangled — the mojibake this client already received once."""
    out = tmp_path / "worksheet.csv"
    write_worksheet([_row()], out)

    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM"

    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert tuple(rows[0]) == HEADER
    assert rows[1][0] == "10001"
    assert rows[1][1] == "馬蹄 ネックレス"
    assert rows[1][4] == "12000", "yen render without a decimal point"


def test_an_empty_result_still_writes_a_usable_file(tmp_path) -> None:
    """Nothing to ask about is a valid outcome; a header-only file says so,
    where a missing file looks like the export failed."""
    out = tmp_path / "empty.csv"
    write_worksheet([], out)
    rows = list(csv.reader(io.StringIO(out.read_bytes().decode("utf-8-sig"))))
    assert tuple(rows[0]) == HEADER
    assert len(rows) == 1


def test_jst_dates_are_rendered_not_timestamps() -> None:
    """A UTC timestamp at 23:00 is the NEXT day in JST; the client reads these
    as order dates."""
    from app.cli.export_unmapped_worksheet import _jst

    assert _jst(datetime(2026, 8, 30, 23, 0, tzinfo=UTC)) == "2026-08-31"
    assert _jst(None) == ""

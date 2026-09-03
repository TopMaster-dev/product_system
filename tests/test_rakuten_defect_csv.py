"""The Rakuten defect CSV puts each identifier in the column it belongs to.

We shipped this file to the client with the 未マッピング実績 block's
商品管理番号 sitting under a "SKU管理番号" heading. They counted 140 rows, went
looking for SKU values that were never in the file, and asked us why assigning
SKU numbers would not resolve them. It would not — Rakuten hands us order lines
keyed on the 商品管理番号 and no SKU-level identifier at all
(`app/adapters/rakuten.py`), so the two blocks genuinely carry different kinds
of identifier and the header has to be honoured per row.

These are unit tests on purpose. The integration test that renders this route
asserts only that the text appears somewhere in the body, which is exactly why
it did not catch a value in the wrong column — and it needs a database, so it
skips on any machine without one.
"""

from __future__ import annotations

import csv
import io
import types

import pytest

pytestmark = pytest.mark.unit


def _fake_report() -> types.SimpleNamespace:
    """Stands in for `rakuten_sku_report` so no database is needed."""
    return types.SimpleNamespace(
        placeholder=[
            {
                "channel_sku": "r-sku00000001",
                "channel_product_id": "10088",
                "master_sku_code": "B45goldbracelet",
                "product_name": "B45 gold",
            }
        ],
        duplicated=[{"channel_sku": "dup-1", "masters": "A,B", "mappings": 2}],
        unmapped_examples=[{"channel_sku": "10001", "lines": 3, "quantity": 5, "amount": 12000}],
    )


async def _render(monkeypatch) -> list[list[str]]:
    import app.ui  # noqa: F401  — import order: package first, avoids a cycle
    from app.ui.routes import data_quality as dq

    async def _report(session, **kwargs):
        return _fake_report()

    monkeypatch.setattr(dq, "rakuten_sku_report", _report)
    response = await dq.rakuten_report_csv(
        operator=None,
        settings=types.SimpleNamespace(rakuten_placeholder_sku_pattern="^r-sku"),
        session=None,
    )
    # utf-8-sig: csv_response prepends a BOM so Excel decodes UTF-8.
    return list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))


def _row(rows: list[list[str]], kind: str) -> dict[str, str]:
    header = rows[0]
    for row in rows[1:]:
        if row and row[0] == kind:
            return dict(zip(header, row, strict=True))
    raise AssertionError(f"no {kind} row emitted")


async def test_unmapped_sales_are_filed_under_product_number(monkeypatch) -> None:
    """The bug the client hit: an order-line identifier under a SKU heading."""
    row = _row(await _render(monkeypatch), "未マッピング実績")
    assert row["商品管理番号"] == "10001"
    assert row["SKU管理番号"] == "", "order lines carry no SKU-level identifier"


async def test_placeholder_rows_keep_both_identifiers(monkeypatch) -> None:
    """仮値 comes from a mapping, which does have both."""
    row = _row(await _render(monkeypatch), "仮値")
    assert row["SKU管理番号"] == "r-sku00000001"
    assert row["商品管理番号"] == "10088"


async def test_placeholder_note_does_not_ask_for_an_rms_edit(monkeypatch) -> None:
    """楽天's SKU管理番号 is immutable — changing it means re-creating the SKU and
    losing its sales ranking — and we never read the field. We withdrew that
    request; the export must not keep issuing it."""
    note = _row(await _render(monkeypatch), "仮値")["備考"]
    assert "変更してください" not in note


async def test_every_block_fills_the_same_number_of_columns(monkeypatch) -> None:
    """A short row silently shifts every value left of it into a wrong header."""
    rows = await _render(monkeypatch)
    width = len(rows[0])
    assert width == 9
    for row in rows[1:]:
        if row:
            assert len(row) == width, f"{row[0]} row is not {width} columns"

"""検収資料の材料 — 金額の基準の判定.

The tax verdict is the one figure in the review material that changes what the
client does with a number: compare against 税込 or 税別 columns of their
受注一覧. A wrong verdict sends them to the wrong column and every comparison
is off by the tax rate. These pin the verdict, and that reading a payload never
reaches the purchaser's personal fields.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from decimal import Decimal
from typing import Any

import pytest

from app.cli.inspect_review_materials import PriceBasis, print_price_basis

pytestmark = pytest.mark.unit


def _line(
    price: str, *, units: int = 1, flag: Any = 1, incl: str | None = None, **kw: Any
) -> dict[str, Any]:
    line: dict[str, Any] = {"price": price, "units": units, "includeTaxFlag": flag}
    if incl is not None:
        line["priceTaxIncl"] = incl
    line.update(kw)
    return line


def _order(*lines: dict[str, Any], **kw: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "PackageModelList": [{"ItemModelList": list(lines), "SenderModel": {"familyName": "山田"}}],
        "OrdererModel": {"familyName": "山田", "phoneNumber1": "090"},
    }
    payload.update(kw)
    return payload


def test_every_line_tax_inclusive_reads_as_tax_inclusive() -> None:
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("3300", incl="3300"), _line("1100", incl="1100")))
    assert basis.rakuten_verdict.startswith("税込")
    assert basis.price_is_tax_incl == basis.with_tax_incl == 2


def test_tax_exclusive_lines_read_as_tax_exclusive() -> None:
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("3000", flag=0, incl="3300")))
    assert basis.rakuten_verdict.startswith("税別")
    assert basis.price_is_tax_incl == 0


def test_a_mixed_shop_is_not_given_a_single_answer() -> None:
    basis = PriceBasis()
    basis.add_rakuten(
        _order(_line("3300", flag=1, incl="3300"), _line("3000", flag=0, incl="3300"))
    )
    assert basis.rakuten_verdict.startswith("混在")


def test_goods_price_is_compared_against_price_times_units() -> None:
    """goodsPrice is RMS's 商品合計金額 — the column the client should match."""
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("1100", units=3), goodsPrice=3300))
    basis.add_rakuten(_order(_line("1100", units=3), goodsPrice=3000))
    assert (basis.goods_price_matches, basis.with_goods_price) == (1, 2)


def test_lines_removed_after_ordering_are_counted_with_their_amount() -> None:
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("1100"), _line("2200", units=2, deleteItemFlag=1)))
    assert basis.deleted_lines == 1
    assert basis.deleted_amount == Decimal("4400")


def test_shipping_and_coupons_are_counted_not_summed() -> None:
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("1100"), postagePrice=550, couponAllTotalPrice=300))
    basis.add_rakuten(_order(_line("1100"), postagePrice=0, couponAllTotalPrice=0))
    assert (basis.with_postage, basis.with_coupon) == (1, 1)


def test_shopify_without_the_flag_says_it_cannot_tell() -> None:
    """Polled orders carry no `taxes_included`. Guessing would be worse than
    saying so."""
    basis = PriceBasis()
    basis.add_shopify({"id": 1})
    assert basis.shopify_verdict.startswith("記録がありません")
    basis.add_shopify({"id": 2, "taxes_included": True})
    assert basis.shopify_verdict.startswith("税込")


def test_the_report_never_prints_purchaser_fields() -> None:
    basis = PriceBasis()
    basis.add_rakuten(_order(_line("1100", incl="1100"), goodsPrice=1100))
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_price_basis(basis)
    out = buf.getvalue()
    assert "山田" not in out
    assert "090" not in out
    assert "税込" in out

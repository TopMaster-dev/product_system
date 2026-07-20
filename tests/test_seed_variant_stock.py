"""Unit tests for app/cli/seed_variant_stock.py (pure aggregator)."""

from __future__ import annotations

import pytest

from app.cli.seed_variant_stock import aggregate_variant_stock, clamp_negatives


@pytest.mark.unit
def test_aggregate_sums_aliases_by_token_color_size() -> None:
    # 006c and N23 are aliases (same token); only one carries stock, so the sum
    # equals that one. anklet/bracelet stay separate keys (different size).
    stock = {
        ("006c", "gold", ""): 27,
        ("N23", "gold", ""): 0,
        ("006c", "silver", ""): 7,
        ("027c", "gold", "anklet"): 50,
        ("027c", "gold", "bracelet"): 50,
    }
    code2token = {"006c": "N23", "N23": "N23", "027c": "B09"}
    out = aggregate_variant_stock(stock, code2token)
    assert out[("N23", "gold", "")] == 27
    assert out[("N23", "silver", "")] == 7
    assert out[("B09", "gold", "anklet")] == 50
    assert out[("B09", "gold", "bracelet")] == 50


@pytest.mark.unit
def test_clamp_negatives_zeroes_negatives_and_reports_them() -> None:
    stock = {
        ("N23", "gold", ""): 27,
        ("R55", "gold", ""): -5,
        ("B09", "silver", "anklet"): 0,
        ("H1", "gold", ""): -1,
    }
    clamped, negatives = clamp_negatives(stock)
    # Negatives are zeroed in the clamped result; non-negatives untouched.
    assert clamped[("R55", "gold", "")] == 0
    assert clamped[("H1", "gold", "")] == 0
    assert clamped[("N23", "gold", "")] == 27
    assert clamped[("B09", "silver", "anklet")] == 0
    # The original negative values are reported for review.
    assert negatives == {("R55", "gold", ""): -5, ("H1", "gold", ""): -1}


@pytest.mark.unit
def test_aggregate_skips_untokened_codes() -> None:
    out = aggregate_variant_stock({("shopify-coupon", "", ""): -1}, {"shopify-coupon": None})
    assert out == {}

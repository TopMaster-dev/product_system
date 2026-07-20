"""Unit tests for app/cli/seed_variant_stock.py (pure aggregator)."""

from __future__ import annotations

import pytest

from app.cli.seed_variant_stock import aggregate_variant_stock


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
def test_aggregate_skips_untokened_codes() -> None:
    out = aggregate_variant_stock({("shopify-coupon", "", ""): -1}, {"shopify-coupon": None})
    assert out == {}

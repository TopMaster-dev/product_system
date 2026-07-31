"""Unit tests for the pure image matcher in app/cli/sync_shopify_images.py."""

from __future__ import annotations

import pytest

from app.cli.sync_shopify_images import resolve_images

pytestmark = pytest.mark.unit


def _v(sku: str, image: str) -> dict[str, str]:
    return {"sku": sku, "image_url": image, "variant_title": "", "product_title": ""}


def test_mapping_match_wins_over_sku_code() -> None:
    variants = [_v("N23gold", "https://cdn/n23.jpg")]
    # The active shopify mapping points at master 1; a same-named master 2 exists.
    out = resolve_images(variants, {"N23gold": 1}, {"N23gold": 2})
    assert out == {1: "https://cdn/n23.jpg"}


def test_falls_back_to_sku_code_when_no_mapping() -> None:
    variants = [_v("B09goldanklet", "https://cdn/b09.jpg")]
    out = resolve_images(variants, {}, {"B09goldanklet": 7})
    assert out == {7: "https://cdn/b09.jpg"}


def test_skips_variants_without_image_or_sku() -> None:
    variants = [_v("N23gold", ""), _v("", "https://cdn/x.jpg"), _v("  ", "  ")]
    assert resolve_images(variants, {"N23gold": 1}, {"N23gold": 1}) == {}


def test_unmatched_variant_is_ignored() -> None:
    out = resolve_images([_v("UNKNOWN", "https://cdn/u.jpg")], {"N23gold": 1}, {"N23gold": 1})
    assert out == {}


def test_first_variant_wins_for_the_same_master() -> None:
    variants = [_v("N23gold", "https://cdn/first.jpg"), _v("N23alias", "https://cdn/second.jpg")]
    out = resolve_images(variants, {"N23gold": 5, "N23alias": 5}, {})
    assert out == {5: "https://cdn/first.jpg"}

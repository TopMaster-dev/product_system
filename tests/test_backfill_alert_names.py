"""Unit tests for the payload name extractor used by the alert backfill."""

from __future__ import annotations

import pytest

from app.cli.backfill_alert_product_names import extract_names

pytestmark = pytest.mark.unit


def test_extract_names_rakuten() -> None:
    raw = {
        "PackageModelList": [
            {
                "ItemModelList": [
                    {
                        "manageNumber": "10113",
                        "itemNumber": "abc",
                        "itemName": "馬蹄ネックレス gold",
                    },
                    {"itemNumber": "b66", "itemName": "フェザーリング silver"},  # no manageNumber
                ]
            }
        ]
    }
    out = extract_names("rakuten", raw)
    assert out["10113"] == "馬蹄ネックレス gold"
    assert out["b66"] == "フェザーリング silver"


def test_extract_names_shopify_graphql_and_rest() -> None:
    graphql = {
        "lineItems": {"edges": [{"node": {"sku": "N23gold", "name": "N23 ネックレス - gold"}}]}
    }
    assert extract_names("shopify", graphql)["N23gold"] == "N23 ネックレス - gold"

    rest = {"line_items": [{"sku": "B09goldanklet", "title": "B09 アンクレット gold"}]}
    assert extract_names("shopify", rest)["B09goldanklet"] == "B09 アンクレット gold"


def test_extract_names_handles_empty_and_bad_payload() -> None:
    assert extract_names("rakuten", None) == {}
    assert extract_names("rakuten", {}) == {}
    assert extract_names("shopify", {"unexpected": True}) == {}

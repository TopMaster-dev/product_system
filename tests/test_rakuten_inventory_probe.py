"""P2-038 probe: read-only by construction, and honest about what it found.

Runs against production with live credentials, so the properties that matter
are pinned here without any network: it only ever sends GET, it never reads a
purchaser field, and it reports a missing permission as a missing permission.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from app.cli import inspect_rakuten_inventory_api as probe
from app.cli.inspect_rakuten_inventory_api import (
    OrderRefs,
    RmsReader,
    VariantRef,
    probe_sample,
    rakuten_error,
    read_item,
    verdict,
)

pytestmark = pytest.mark.unit


def _payload(*lines: dict[str, Any]) -> dict[str, Any]:
    return {
        "OrdererModel": {"familyName": "山田", "phoneNumber1": "090-0000-0000"},
        "PackageModelList": [{"SenderModel": {"familyName": "山田"}, "ItemModelList": list(lines)}],
    }


def _line(mn: str, vid: str = "", merchant: str = "") -> dict[str, Any]:
    return {
        "manageNumber": mn,
        "SkuModelList": [{"variantId": vid, "merchantDefinedSkuId": merchant}],
    }


# --- 受注データから読むもの -------------------------------------------------


def test_only_product_identity_is_kept() -> None:
    refs = OrderRefs()
    refs.add_payload(_payload(_line("10002", "sku-1", "10002goldanklet")))
    kept = repr(refs)
    assert "山田" not in kept and "090" not in kept
    assert refs.refs[("10002", "sku-1")] == VariantRef("10002", "sku-1", "10002goldanklet")


def test_a_line_known_only_by_its_merchant_id_is_still_checked() -> None:
    refs = OrderRefs()
    refs.add_payload(_payload(_line("B66", "", "B66-g-m"), _line("X1")))
    assert refs.lines == 2
    assert (refs.with_variant_id, refs.with_merchant_sku_id) == (0, 1)
    assert list(refs.refs) == [("B66", "#B66-g-m")]


def test_the_sample_spreads_over_products_first() -> None:
    refs = OrderRefs()
    refs.add_payload(
        _payload(_line("A", "a1"), _line("A", "a2"), _line("A", "a3"), _line("B", "b1"))
    )
    picked = refs.sample(3)
    assert [r.manage_number for r in picked[:2]] == ["A", "B"]
    assert len(picked) == 3


# --- 商品APIの読み方 -------------------------------------------------------


ITEM = {
    "manageNumber": "10002",
    "variants": {
        "sku-1": {"merchantDefinedSkuId": "10002goldanklet", "selectorValues": {"色": "gold"}},
        "sku-2": {"merchantDefinedSkuId": "10002silveranklet"},
    },
}


def test_a_sku_is_found_by_its_variant_id() -> None:
    view = read_item(ITEM, VariantRef("10002", "sku-1", ""))
    assert (view.variant_id, view.matched_by, view.variant_count) == ("sku-1", "variantId", 2)
    assert view.merchant_sku_id == "10002goldanklet"


def test_a_sku_is_found_by_its_merchant_id_when_the_order_lacks_a_variant_id() -> None:
    view = read_item(ITEM, VariantRef("10002", "", "10002silveranklet"))
    assert (view.variant_id, view.matched_by) == ("sku-2", "merchantDefinedSkuId")


def test_an_unknown_sku_is_reported_as_not_found() -> None:
    view = read_item(ITEM, VariantRef("10002", "sku-9", "nope"))
    assert view.variant_id == "" and view.variant_count == 2


def test_rakuten_errors_are_read_in_every_shape() -> None:
    assert rakuten_error({"errors": [{"code": "GE0014", "message": "not found"}]}) == (
        "GE0014 not found"
    )
    assert rakuten_error({"errorCode": "ES04-01", "message": "bad"}) == "ES04-01 bad"
    assert rakuten_error("oops") == ""


def test_a_missing_permission_is_called_a_missing_permission() -> None:
    assert verdict([403, 403]).startswith("権限なし")
    assert verdict([401]).startswith("認証エラー")
    assert verdict([200, 404]).startswith("利用可能")
    assert verdict([]) == "未確認"


# --- HTTP: GET のみ ----------------------------------------------------------


class _Recorder:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, response in self.routes.items():
            if fragment in request.url.path:
                return response
        return httpx.Response(404, json={"errors": [{"code": "X404", "message": "no route"}]})


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "CALL_INTERVAL", 0)


async def _run(routes: dict[str, httpx.Response], ref: VariantRef) -> tuple[Any, _Recorder]:
    recorder = _Recorder(routes)
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        rms = RmsReader(service_secret=" secret\r\n", license_key="key\r\n", client=client)
        result = await probe_sample(rms, ref)
    return result, recorder


async def test_a_found_sku_is_looked_up_in_the_inventory_api() -> None:
    result, rec = await _run(
        {
            "/items/manage-numbers/10002": httpx.Response(200, json=ITEM),
            "/inventories/manage-numbers/10002/variants/sku-2": httpx.Response(
                200, json={"quantity": 5}
            ),
        },
        VariantRef("10002", "", "10002silveranklet"),
    )
    assert result.matched and result.item_status == 200 and result.inventory_status == 200
    assert "在庫 5" in result.line
    assert all(r.method == "GET" for r in rec.requests)
    token = base64.b64encode(b"secret:key").decode()
    assert rec.requests[0].headers["Authorization"] == f"ESA {token}"


async def test_a_forbidden_item_api_is_reported_and_nothing_else_is_guessed() -> None:
    result, rec = await _run(
        {
            "/items/": httpx.Response(
                403, json={"errors": [{"code": "AU0001", "message": "forbidden"}]}
            )
        },
        VariantRef("10002", "", "10002goldanklet"),
    )
    assert result.item_status == 403 and "AU0001" in result.line
    # No SKU id known and none confirmed: the inventory API is not asked.
    assert result.inventory_status is None
    assert len(rec.requests) == 1

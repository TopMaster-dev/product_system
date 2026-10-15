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
    assert verdict([200, 404], baseline_ok=True).startswith(probe.GRANTED)
    assert verdict([], baseline_ok=True) == "未確認"
    # RMS never answers 403 here; a refused permission arrives as 401.
    assert probe.NOT_GRANTED in verdict([401, 401], baseline_ok=True)


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


# --- ゲートウェイ応答の分類 (2026-10-12 に実測) -----------------------------


@pytest.mark.parametrize(
    ("status", "server", "code", "expected"),
    [
        # Measured against api.rms.rakuten.co.jp with a BOGUS token, so routing
        # could be read with no credential in play.
        (404, "Apache", "", probe.NOT_A_ROUTE),  # /es/2.0/inventory/updateInventory/
        (404, "istio-envoy", "GF0002 Not Found", probe.NOT_AN_ENDPOINT),
        (400, "istio-envoy", "GF0001 Bad Request", probe.BAD_PATH_PARAM),
        (401, "istio-envoy", "GA0001 Un-Authorised", probe.NOT_GRANTED),
        (200, "istio-envoy", "", probe.GRANTED),
        (400, "istio-envoy", "GK0001", probe.NO_AUTH_HEADER),
        (405, "istio-envoy", "", probe.BAD_METHOD),
        (400, "istio-envoy", "XX9999 whatever", probe.BAD_BODY),
    ],
)
def test_each_gateway_answer_means_one_thing(
    status: int, server: str, code: str, expected: str
) -> None:
    assert probe.classify(status, server, code) == expected


def test_the_production_push_url_is_not_a_rakuten_route() -> None:
    """app/adapters/rakuten.py posts stock to /es/2.0/inventory/updateInventory/.
    Measured 2026-10-12: that path answers 404 from Apache — the same class as a
    URL invented on the spot. It is not an RMS endpoint, which is why the stock
    push has never once succeeded in production."""
    assert probe.classify(404, "Apache", "") == probe.NOT_A_ROUTE
    assert probe.classify(404, "Apache", "") != probe.NOT_GRANTED


def test_a_401_is_a_permission_not_a_credential_when_the_baseline_works() -> None:
    """The first version blamed the credentials for every 401, which would have
    sent the client chasing a key that the same run proves is fine."""
    with_baseline = probe.verdict([401, 401], baseline_ok=True)
    assert probe.NOT_GRANTED in with_baseline
    assert "利用機能" in with_baseline

    alone = probe.verdict([401], baseline_ok=False)
    assert "切り分け不能" in alone


def test_the_grant_map_names_the_three_branches_it_cannot_separate() -> None:
    refused = {
        probe.BASELINE_FAMILY: probe.Probe(200, {"x": 1}, server="istio-envoy"),
        "在庫API 2.0 単体取得": probe.Probe(401, None, "GA0001", "istio-envoy"),
        "在庫API 2.0 一括取得": probe.Probe(401, None, "GA0001", "istio-envoy"),
    }
    branch = probe.scope_branch(refused)
    assert "認証情報は正常" in branch
    for marker in ("(a)", "(b)", "(c)"):
        assert marker in branch

    granted = dict(refused)
    granted["在庫API 2.0 単体取得"] = probe.Probe(200, {"quantity": 5}, server="istio-envoy")
    assert "既に利用可能" in probe.scope_branch(granted)

    assert "判定不能" in probe.scope_branch({probe.BASELINE_FAMILY: probe.Probe(401, None)})


# --- 書き込みには到達できない -----------------------------------------------


async def test_the_probe_refuses_to_post_anywhere_that_writes() -> None:
    """bulk-upsert SETS stock. A typo in a manage number there would zero a live
    product, so the probe cannot reach it at all — not by policy, by construction."""
    recorder = _Recorder({})
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        rms = RmsReader(service_secret="s", license_key="k", client=client)
        with pytest.raises(ValueError, match="may not POST"):
            await rms.call("POST", "/2.0/inventories/bulk-upsert", {"inventories": []})
        await rms.call("POST", "/2.0/inventories/bulk-get", {"inventories": []})
    assert [r.method for r in recorder.requests] == ["POST"]
    assert "bulk-upsert" not in str(recorder.requests[0].url)


async def test_every_grant_call_is_a_read() -> None:
    writes = [
        c
        for c in probe.grant_calls("10014", "1542")
        if c.method != "GET" and c.path not in RmsReader.READ_ONLY_POSTS
    ]
    assert not writes, f"these would write: {[c.path for c in writes]}"


def test_the_controls_come_before_the_questions() -> None:
    """The run proves its own discriminators before leaning on them."""
    calls = probe.grant_calls("10014", "1542")
    assert calls[0].family == probe.BASELINE_FAMILY
    assert calls[0].control.startswith("基準")
    controls = [i for i, c in enumerate(calls) if c.control]
    questions = [i for i, c in enumerate(calls) if not c.control]
    assert max(controls) < min(questions)
    # The licence key must never travel in a URL, where proxies log it.
    assert not any("licenseKey" in c.path for c in calls)


# --- 応答の形を読み取る -------------------------------------------------------


def test_a_success_reports_the_field_names_we_must_send() -> None:
    """Rakuten's spec is behind a login, so a real 200 is how we learn what the
    stock write has to carry. The probe prints the shape, not just the status."""
    single = probe.Probe(200, {"quantity": 7, "manageNumber": "10014", "variantId": "1542"})
    assert "quantity=7" in single.summary

    bulk = probe.Probe(
        200,
        {"inventories": [{"manageNumber": "10014", "variantId": "1542", "quantity": 7}]},
    )
    shape = bulk.summary
    assert "inventories[1]" in shape
    for field in ("manageNumber", "variantId", "quantity"):
        assert field in shape


def test_an_error_still_wins_over_the_shape() -> None:
    assert probe.Probe(401, None, "GA0001 Un-Authorised").summary.startswith("GA0001")
    assert probe.Probe(404, None, "", "Apache").summary == ""


def test_the_preview_cannot_run_away() -> None:
    """A whole-catalogue range read returns hundreds of rows; the line stays one line."""
    big = probe.Probe(200, {"inventories": [{"k": "v" * 50} for _ in range(400)]})
    assert len(big.summary) <= probe.BODY_PREVIEW
    assert "inventories[400]" in big.summary

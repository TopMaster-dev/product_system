"""RakutenAdapter unit tests — search + getOrder normalization via MockTransport."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters import RakutenAdapter
from app.adapters.rate_limit import TokenBucket


def _adapter(client: httpx.AsyncClient | None = None) -> RakutenAdapter:
    return RakutenAdapter(
        service_secret="srv",
        license_key="lic",
        shop_url="https://www.rakuten.co.jp/shop/",
        client=client,
        # Wide bucket so unit tests are not slowed by the limiter.
        rate_limiter=TokenBucket(rate=1000, capacity=1000),
    )


@pytest.mark.unit
def test_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError):
        RakutenAdapter(service_secret="", license_key="x")
    with pytest.raises(ValueError):
        RakutenAdapter(service_secret="x", license_key="")


@pytest.mark.unit
def test_auth_header_uses_esa_base64() -> None:
    adapter = _adapter()
    header = adapter._auth_header()["Authorization"]
    assert header.startswith("ESA ")
    decoded = base64.b64decode(header[4:]).decode()
    assert decoded == "srv:lic"


@pytest.mark.unit
async def test_verify_webhook_is_noop_true() -> None:
    """Rakuten has no webhooks in Phase 1-A; the verifier returns True."""
    assert _adapter().verify_webhook({}, b"")


@pytest.mark.unit
async def test_push_inventory_implemented_in_phase1b() -> None:
    """Phase 1-B F1.5: implementation lives in test_rakuten_push_inventory.py.

    This sanity test only confirms the method exists and rejects negative
    quantities synchronously (no HTTP call required for the negative path).
    """
    with pytest.raises(ValueError, match="negative"):
        await _adapter().push_inventory("SKU", -1)


@pytest.mark.unit
async def test_fetch_orders_normalizes_response() -> None:
    search_response = {"orderNumberList": ["240101-12345678-0001"]}
    get_response = {
        "OrderModelList": [
            {
                "orderNumber": "240101-12345678-0001",
                "orderProgress": 300,
                "orderDatetime": "2026-05-11T10:00:00+09:00",
                "PackageModelList": [
                    {
                        "ItemModelList": [
                            {
                                "itemDetailId": 11,
                                "manageNumber": "10087goldS",
                                "itemNumber": "10111",
                                "units": 2,
                                "price": "3000",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    responses = iter([search_response, get_response])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))

    assert len(orders) == 1
    order = orders[0]
    assert order.channel == "rakuten"
    assert order.channel_order_id == "240101-12345678-0001"
    assert order.status == "confirmed"  # 300 = 発送待ち
    assert len(order.items) == 1
    assert order.items[0].channel_sku == "10087goldS"
    assert order.items[0].quantity == 2


@pytest.mark.unit
async def test_fetch_orders_empty_search_short_circuits() -> None:
    """An empty search response must NOT call getOrder."""
    calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"orderNumberList": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        assert await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC)) == []
    assert calls["count"] == 1


@pytest.mark.unit
async def test_cancelled_status_normalizes() -> None:
    search_response = {"orderNumberList": ["X-1"]}
    get_response = {
        "OrderModelList": [
            {
                "orderNumber": "X-1",
                "orderProgress": 800,
                "orderDatetime": "2026-05-11T10:00:00+09:00",
                "PackageModelList": [],
            }
        ]
    }
    responses = iter([search_response, get_response])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))
    assert orders[0].status == "cancelled"

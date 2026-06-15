"""ShopifyAdapter unit tests — HMAC verification and webhook normalization.

GraphQL fetch is covered with a mocked httpx transport.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.adapters import ShopifyAdapter

SHOP_DOMAIN = "test.myshopify.com"
SECRET = "shpss_test_secret"
TOKEN = "shpat_test"


def _adapter(client: httpx.AsyncClient | None = None) -> ShopifyAdapter:
    return ShopifyAdapter(
        shop_domain=SHOP_DOMAIN,
        access_token=TOKEN,
        webhook_secret=SECRET,
        client=client,
    )


@pytest.mark.unit
def test_hmac_valid_signature_accepted() -> None:
    body = b'{"id": 1}'
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    adapter = _adapter()
    assert adapter.verify_webhook({"x-shopify-hmac-sha256": sig}, body)


@pytest.mark.unit
def test_hmac_invalid_signature_rejected() -> None:
    body = b'{"id": 1}'
    adapter = _adapter()
    assert not adapter.verify_webhook(
        {"x-shopify-hmac-sha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        body,
    )


@pytest.mark.unit
def test_hmac_missing_header_rejected() -> None:
    assert not _adapter().verify_webhook({}, b'{"id": 1}')


@pytest.mark.unit
def test_hmac_modified_body_rejected() -> None:
    body = b'{"id": 1}'
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert not _adapter().verify_webhook({"x-shopify-hmac-sha256": sig}, b'{"id": 2}')


@pytest.mark.unit
def test_hmac_header_lookup_is_case_insensitive() -> None:
    body = b"x"
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert _adapter().verify_webhook({"X-Shopify-Hmac-Sha256": sig}, body)


@pytest.mark.unit
def test_normalize_webhook_order_with_cancellation() -> None:
    payload = {
        "id": 12345,
        "name": "#1001",
        "cancelled_at": "2026-05-11T03:00:00Z",
        "fulfillment_status": None,
        "created_at": "2026-05-11T02:00:00Z",
        "currency": "JPY",
        "line_items": [
            {
                "id": 9991,
                "sku": "10087goldS",
                "quantity": 2,
                "variant_id": 444,
                "price": "3000",
                "price_set": {"shop_money": {"currency_code": "JPY"}},
            }
        ],
    }
    normalized = ShopifyAdapter.normalize_webhook_order(payload)
    assert normalized.channel == "shopify"
    assert normalized.channel_order_id == "12345"
    assert normalized.status == "cancelled"
    assert len(normalized.items) == 1
    assert normalized.items[0].channel_sku == "10087goldS"
    assert normalized.items[0].quantity == 2


@pytest.mark.unit
def test_normalize_webhook_order_fulfilled_maps_to_shipped() -> None:
    payload = {
        "id": 1,
        "fulfillment_status": "fulfilled",
        "created_at": "2026-05-11T02:00:00Z",
        "line_items": [],
    }
    assert ShopifyAdapter.normalize_webhook_order(payload).status == "shipped"


@pytest.mark.unit
async def test_fetch_orders_walks_pagination() -> None:
    """GraphQL fetcher walks pageInfo.hasNextPage until exhausted."""
    page1 = {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Order/1",
                            "name": "#1",
                            "createdAt": "2026-05-11T01:00:00Z",
                            "updatedAt": "2026-05-11T01:00:00Z",
                            "cancelledAt": None,
                            "displayFulfillmentStatus": "UNFULFILLED",
                            "displayFinancialStatus": "PAID",
                            "lineItems": {"edges": []},
                        }
                    }
                ],
            }
        }
    }
    page2 = {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Order/2",
                            "name": "#2",
                            "createdAt": "2026-05-11T02:00:00Z",
                            "updatedAt": "2026-05-11T02:00:00Z",
                            "cancelledAt": None,
                            "displayFulfillmentStatus": "UNFULFILLED",
                            "displayFinancialStatus": "PAID",
                            "lineItems": {"edges": []},
                        }
                    }
                ],
            }
        }
    }
    responses = iter([page1, page2])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        from datetime import UTC, datetime

        orders = await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))
        # v0.2.2 _strip_gid normalizes GraphQL `gid://shopify/Order/N` -> `N`
        # so the polling path matches the webhook path's numeric channel_order_id
        # and the (channel, channel_order_id) UNIQUE prevents duplicates.
        assert [o.channel_order_id for o in orders] == ["1", "2"]


@pytest.mark.unit
async def test_fetch_orders_raises_on_graphql_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "throttled"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client=client)
        from datetime import UTC, datetime

        with pytest.raises(RuntimeError, match="throttled"):
            await adapter.fetch_orders(since=datetime(2026, 5, 11, tzinfo=UTC))


@pytest.mark.unit
async def test_push_inventory_not_implemented_in_phase1a() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter().push_inventory("SKU-1", 5)


@pytest.mark.unit
def test_sample_webhook_round_trip() -> None:
    """A real Shopify webhook body verifies against the same secret used to sign it."""
    body = json.dumps({"id": 1, "line_items": []}).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert _adapter().verify_webhook({"x-shopify-hmac-sha256": sig}, body)

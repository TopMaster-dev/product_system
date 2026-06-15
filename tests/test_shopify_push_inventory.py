"""Unit tests for ShopifyAdapter.push_inventory (Phase 1-B F1.6).

Use httpx.MockTransport to script the multi-call sequence:
1. (optional) primary-location auto-discovery
2. inventoryItems by sku lookup
3. inventorySetOnHandQuantities mutation
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.rate_limit import TokenBucket
from app.adapters.shopify import ShopifyAdapter


def _adapter(handler, *, location_id: str = "") -> tuple[ShopifyAdapter, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def _wrap(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req, captured)

    transport = httpx.MockTransport(_wrap)
    client = httpx.AsyncClient(transport=transport)
    a = ShopifyAdapter(
        shop_domain="test.myshopify.com",
        access_token="tok",
        webhook_secret="wh",
        client=client,
        rate_limiter=TokenBucket(rate=100, capacity=1000),
        location_id=location_id,
    )
    return a, captured


def _gql_body(req: httpx.Request) -> dict:
    return json.loads(req.content)


def _route(req: httpx.Request, _captured: list[httpx.Request]) -> str:
    """Identify which GraphQL operation a request carries."""
    body = _gql_body(req)
    q = body.get("query", "")
    if "PrimaryLocation" in q:
        return "primary_location"
    if "InventoryItemBySku" in q:
        return "lookup_sku"
    if "InventorySet" in q:
        return "mutation"
    return "other"


# ---------- happy path ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_full_three_call_flow() -> None:
    def handler(req: httpx.Request, captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [
                                {"node": {"id": "gid://shopify/Location/123", "name": "Main"}}
                            ]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [
                                {"node": {"id": "gid://shopify/InventoryItem/999", "sku": "ABC"}}
                            ]
                        }
                    }
                },
            )
        if kind == "mutation":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventorySetOnHandQuantities": {
                            "inventoryAdjustmentGroup": {
                                "id": "gid://shopify/InventoryAdjustmentGroup/1",
                                "createdAt": "2026-06-16T00:00:00Z",
                            },
                            "userErrors": [],
                        }
                    }
                },
            )
        raise AssertionError(f"unexpected GraphQL op: {kind}")

    a, captured = _adapter(handler)
    async with a:
        result = await a.push_inventory("ABC", 42)

    # 3 calls: location + lookup + mutation
    assert len(captured) == 3
    routes = [_route(r, captured) for r in captured]
    assert routes == ["primary_location", "lookup_sku", "mutation"]
    assert isinstance(result, dict)
    assert result.get("inventoryAdjustmentGroup", {}).get("id")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_uses_supplied_location_skipping_discovery() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "lookup_sku":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [{"node": {"id": "gid://shopify/InventoryItem/1", "sku": "X"}}]
                        }
                    }
                },
            )
        if kind == "mutation":
            return httpx.Response(
                200,
                json={"data": {"inventorySetOnHandQuantities": {"userErrors": []}}},
            )
        raise AssertionError("unexpected call when location_id is pre-configured")

    a, captured = _adapter(handler, location_id="gid://shopify/Location/77")
    async with a:
        await a.push_inventory("X", 1)

    # Only 2 calls: lookup + mutation (no location auto-discovery)
    assert len(captured) == 2
    # The mutation must carry the configured location, not a discovered one
    mutation_body = _gql_body(captured[1])
    set_q = mutation_body["variables"]["input"]["setQuantities"][0]
    assert set_q["locationId"] == "gid://shopify/Location/77"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_location_caches_across_calls() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [{"node": {"id": "gid://shopify/Location/55", "name": "M"}}]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [{"node": {"id": "gid://shopify/InventoryItem/2", "sku": "Y"}}]
                        }
                    }
                },
            )
        if kind == "mutation":
            return httpx.Response(
                200,
                json={"data": {"inventorySetOnHandQuantities": {"userErrors": []}}},
            )
        raise AssertionError("?")

    a, captured = _adapter(handler)
    async with a:
        await a.push_inventory("Y", 1)
        await a.push_inventory("Y", 2)

    routes = [_route(r, captured) for r in captured]
    # First push triggers location query; second push reuses it
    assert routes.count("primary_location") == 1
    assert routes.count("mutation") == 2


# ---------- error paths ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_rejects_negative_quantity() -> None:
    a, _ = _adapter(lambda req, c: httpx.Response(200, json={}))
    async with a:
        with pytest.raises(ValueError, match="negative"):
            await a.push_inventory("X", -1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_raises_when_sku_not_found() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [{"node": {"id": "gid://shopify/Location/1", "name": "M"}}]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            return httpx.Response(200, json={"data": {"inventoryItems": {"edges": []}}})
        raise AssertionError("mutation should not be called when SKU not found")

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="not found"):
            await a.push_inventory("MISSING", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_raises_on_ambiguous_sku() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [{"node": {"id": "gid://shopify/Location/1", "name": "M"}}]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [
                                {"node": {"id": "gid://shopify/InventoryItem/1", "sku": "DUP"}},
                                {"node": {"id": "gid://shopify/InventoryItem/2", "sku": "DUP"}},
                            ]
                        }
                    }
                },
            )
        raise AssertionError("mutation should not be called when SKU is ambiguous")

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="multiple"):
            await a.push_inventory("DUP", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_raises_on_user_errors() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [{"node": {"id": "gid://shopify/Location/1", "name": "M"}}]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [{"node": {"id": "gid://shopify/InventoryItem/9", "sku": "X"}}]
                        }
                    }
                },
            )
        if kind == "mutation":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventorySetOnHandQuantities": {
                            "userErrors": [
                                {
                                    "field": ["input", "setQuantities", "0", "quantity"],
                                    "message": "Quantity must be greater than or equal to 0",
                                    "code": "INVALID",
                                }
                            ]
                        }
                    }
                },
            )
        raise AssertionError("?")

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match=r"INVALID|Quantity"):
            await a.push_inventory("X", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_raises_on_graphql_top_level_errors() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "throttled"}]})

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            await a.push_inventory("X", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_raises_when_no_active_location() -> None:
    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(200, json={"data": {"locations": {"edges": []}}})
        raise AssertionError("subsequent calls should be skipped")

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="auto-discovery"):
            await a.push_inventory("X", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sku_query_quotes_are_escaped() -> None:
    """A SKU containing a quote should not break the GraphQL `sku:"<v>"` literal."""
    captured_q: list[str] = []

    def handler(req: httpx.Request, _captured: list[httpx.Request]) -> httpx.Response:
        kind = _route(req, _captured)
        if kind == "primary_location":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "locations": {
                            "edges": [{"node": {"id": "gid://shopify/Location/1", "name": "M"}}]
                        }
                    }
                },
            )
        if kind == "lookup_sku":
            body = _gql_body(req)
            captured_q.append(body["variables"]["q"])
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventoryItems": {
                            "edges": [{"node": {"id": "gid://shopify/InventoryItem/1", "sku": "x"}}]
                        }
                    }
                },
            )
        if kind == "mutation":
            return httpx.Response(
                200,
                json={"data": {"inventorySetOnHandQuantities": {"userErrors": []}}},
            )
        raise AssertionError("?")

    a, _ = _adapter(handler)
    async with a:
        await a.push_inventory('A"B', 5)

    assert captured_q == ['sku:"A\\"B"']

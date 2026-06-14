"""Unit tests for RakutenAdapter.push_inventory (Phase 1-B F1.5)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.rakuten import RakutenAdapter
from app.adapters.rate_limit import TokenBucket


def _adapter(handler) -> tuple[RakutenAdapter, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def _wrap(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_wrap)
    client = httpx.AsyncClient(transport=transport)
    a = RakutenAdapter(
        service_secret="ss",
        license_key="lk",
        client=client,
        # capacity=999 so test does not throttle; bursts are not tested here
        rate_limiter=TokenBucket(rate=100, capacity=1000),
    )
    return a, captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_posts_set_operation() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Results": {}})

    a, captured = _adapter(handler)
    async with a:
        result = await a.push_inventory("MYSKU-123", 42)

    assert isinstance(result, dict)
    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == "/es/2.0/inventory/updateInventory/"
    body = json.loads(req.content)
    items = body["inventoryUpdateRequestRakutenItem"]
    assert items[0]["manageNumber"] == "MYSKU-123"
    assert items[0]["inventory"] == 42
    assert items[0]["inventoryType"] == 1   # 通常在庫
    assert items[0]["inventoryOperation"] == 1  # SET


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_uses_esa_auth_header() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    a, captured = _adapter(handler)
    async with a:
        await a.push_inventory("X", 1)

    auth = captured[0].headers.get("authorization") or ""
    assert auth.startswith("ESA ")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_rejects_negative_quantity() -> None:
    a, _ = _adapter(lambda r: httpx.Response(200, json={}))
    async with a:
        with pytest.raises(ValueError, match="negative"):
            await a.push_inventory("X", -1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_raises_on_http_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(httpx.HTTPStatusError):
            await a.push_inventory("X", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_raises_on_rakuten_error_code() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Results": {"errorCode": "ES04-01", "message": "Bad Request"}},
        )

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="ES04-01"):
            await a.push_inventory("BADSKU", 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_raises_on_per_item_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "inventoryUpdateResponseItem": [
                    {"manageNumber": "X", "errorCode": "ITEM-NOT-FOUND",
                     "message": "manageNumber not found"}
                ]
            },
        )

    a, _ = _adapter(handler)
    async with a:
        with pytest.raises(RuntimeError, match="ITEM-NOT-FOUND"):
            await a.push_inventory("X", 5)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_inventory_strips_credential_whitespace() -> None:
    """Re-tests the Phase 1-A CR/LF fix on the new code path."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    a = RakutenAdapter(
        service_secret="ss\r",
        license_key="lk\n",
        client=client,
        rate_limiter=TokenBucket(rate=100, capacity=1000),
    )
    async with a:
        await a.push_inventory("X", 1)
    # If \r leaked into the token, the header would have a newline; check no CRLF
    auth = captured[0].headers.get("authorization") or ""
    assert "\r" not in auth
    assert "\n" not in auth

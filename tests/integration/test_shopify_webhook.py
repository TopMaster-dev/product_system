"""Integration tests — POST /webhooks/shopify end-to-end.

Uses httpx ASGITransport so the FastAPI app runs in the same event loop as
the test, avoiding asyncpg "different loop" errors that TestClient triggers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.adapters import ShopifyAdapter
from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    MasterSku,
    Order,
    WebhookLog,
    WebhookStatusEnum,
)
from app.queue import InMemoryTaskQueue, get_task_queue, reset_task_queue
from app.services.handlers import register_handlers

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "shpss_test_secret"


def _sign(body: bytes) -> str:
    return base64.b64encode(
        hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()


def _webhook_body(order_id: str, sku: str, quantity: int = 1, *, cancelled: bool = False) -> bytes:
    return json.dumps(
        {
            "id": order_id,
            "name": f"#{order_id}",
            "cancelled_at": "2026-05-11T03:00:00Z" if cancelled else None,
            "fulfillment_status": None,
            "created_at": "2026-05-11T02:00:00Z",
            "currency": "JPY",
            "line_items": [
                {
                    "id": "L-1",
                    "sku": sku,
                    "quantity": quantity,
                    "variant_id": 1,
                    "price": "1000",
                }
            ],
        }
    ).encode()


@pytest.fixture
async def webhook_client(_test_engine) -> AsyncIterator[AsyncClient]:
    """AsyncClient with DB + queue dependencies pinned to the test engine."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def _override_session():
        async with factory() as session:
            yield session

    test_settings = Settings(
        app_env="local",
        shopify_shop_domain="test.myshopify.com",
        shopify_access_token="t",
        shopify_webhook_secret=WEBHOOK_SECRET,
    )
    queue = InMemoryTaskQueue()
    register_handlers(queue, factory)
    reset_task_queue()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_task_queue] = lambda: queue
    from app.api.webhooks import get_settings as ep_get_settings

    app.dependency_overrides[ep_get_settings] = lambda: test_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    reset_task_queue()


async def _seed_mapping(session_factory: async_sessionmaker, sku: str) -> int:
    async with session_factory() as session, session.begin():
        master = MasterSku(sku_code=f"MASTER-{sku}", name=sku)
        session.add(master)
        await session.flush()
        session.add(
            ChannelSkuMapping(
                master_sku_id=master.id,
                channel="shopify",
                channel_sku=sku,
                is_active=True,
            )
        )
        return master.id


async def test_valid_webhook_ingests_order_and_decrements(webhook_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    master_id = await _seed_mapping(factory, "W-SKU")

    body = _webhook_body("HOOK-1", "W-SKU", quantity=2)
    headers = {
        ShopifyAdapter.HEADER_HMAC: _sign(body),
        ShopifyAdapter.HEADER_WEBHOOK_ID: "wh-1",
        ShopifyAdapter.HEADER_TOPIC: "orders/create",
        "Content-Type": "application/json",
    }
    response = await webhook_client.post("/webhooks/shopify", content=body, headers=headers)
    assert response.status_code == 200

    async with factory() as session:
        order = (
            await session.execute(select(Order).where(Order.channel_order_id == "HOOK-1"))
        ).scalar_one()
        assert order.status == "confirmed"
        event = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == master_id)
            )
        ).scalar_one()
        assert event.quantity_delta == -2
        webhook_row = (
            await session.execute(select(WebhookLog).where(WebhookLog.webhook_id == "wh-1"))
        ).scalar_one()
        assert webhook_row.hmac_valid is True
        assert webhook_row.status == WebhookStatusEnum.PROCESSED


async def test_invalid_hmac_returns_401_and_logs_rejection(webhook_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    body = _webhook_body("HOOK-2", "ANY-SKU")
    headers = {
        ShopifyAdapter.HEADER_HMAC: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ShopifyAdapter.HEADER_WEBHOOK_ID: "wh-2",
        ShopifyAdapter.HEADER_TOPIC: "orders/create",
    }
    response = await webhook_client.post("/webhooks/shopify", content=body, headers=headers)
    assert response.status_code == 401

    async with factory() as session:
        row = (
            await session.execute(select(WebhookLog).where(WebhookLog.webhook_id == "wh-2"))
        ).scalar_one()
        assert row.hmac_valid is False
        assert row.status == WebhookStatusEnum.REJECTED

        # No order or events were created.
        orders = (
            await session.execute(select(Order).where(Order.channel_order_id == "HOOK-2"))
        ).all()
        assert orders == []


async def test_duplicate_webhook_id_is_acked_without_double_processing(
    webhook_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    master_id = await _seed_mapping(factory, "DUP-SKU")

    body = _webhook_body("HOOK-3", "DUP-SKU", quantity=1)
    sig = _sign(body)
    headers = {
        ShopifyAdapter.HEADER_HMAC: sig,
        ShopifyAdapter.HEADER_WEBHOOK_ID: "wh-3",
        ShopifyAdapter.HEADER_TOPIC: "orders/create",
        "Content-Type": "application/json",
    }
    r1 = await webhook_client.post("/webhooks/shopify", content=body, headers=headers)
    r2 = await webhook_client.post("/webhooks/shopify", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200

    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == master_id)
                )
            )
            .scalars()
            .all()
        )
        # Idempotency UNIQUE on event source — even if the same order goes
        # through the pipeline twice, only one consumption event lands.
        assert len(events) == 1

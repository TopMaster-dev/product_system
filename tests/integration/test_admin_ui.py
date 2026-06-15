"""Integration tests — admin UI end-to-end via ASGITransport.

Covers Basic Auth, every screen renders, and the high-value mutations
(manual adjust, mapping create/delete, alert resolution) wire through
to the underlying services.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventoryEventTypeEnum,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)

pytestmark = pytest.mark.integration

USER = "admin"
PASSWORD = "test_secret"


def _auth_header(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def admin_client(_test_engine) -> AsyncIterator[AsyncClient]:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def _override_session():
        async with factory() as session:
            yield session

    test_settings = Settings(
        app_env="local",
        admin_username=USER,
        admin_password=PASSWORD,
    )
    app.dependency_overrides[get_session] = _override_session
    from app.ui.auth import get_settings as auth_get_settings

    app.dependency_overrides[auth_get_settings] = lambda: test_settings
    from app.ui.routes.home import (
        get_session as home_get_session,  # noqa: F401 (proves no override clash)
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _seed_sku(factory, code: str = "T-1", name: str = "Test") -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=name)
        session.add(sku)
        await session.flush()
        return sku.id


async def test_unauthenticated_request_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


async def test_wrong_password_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header(password="bad"))
    assert r.status_code == 401


async def test_home_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header())
    assert r.status_code == 200
    assert "ダッシュボード" in r.text
    assert "operator:" in r.text


async def test_inventory_list_filters_and_paginates(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "INV-A", "Apple")
    await _seed_sku(factory, "INV-B", "Banana")

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert r.status_code == 200
    assert "INV-A" in r.text and "INV-B" in r.text

    # Search narrows.
    r = await admin_client.get("/admin/inventory?q=Banana", headers=_auth_header())
    assert "INV-A" not in r.text
    assert "INV-B" in r.text


async def test_mapping_create_and_delete(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "M-1", "Mapped")

    r = await admin_client.post(
        "/admin/mappings/new",
        data={
            "master_sku_id": sku_id,
            "channel": "shopify",
            "channel_sku": "SHOP-001",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        mapping = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.channel_sku == "SHOP-001")
            )
        ).scalar_one()
        mapping_id = mapping.id

    r = await admin_client.post(f"/admin/mappings/{mapping_id}/delete", headers=_auth_header())
    assert r.status_code == 303

    async with factory() as session:
        rows = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.id == mapping_id)
            )
        ).all()
        assert rows == []


async def test_mapping_csv_export(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "E-1", "ExportMe")
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=sku_id, channel="shopify", channel_sku="EXP-1", is_active=True
            )
        )

    r = await admin_client.get("/admin/mappings/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "E-1,shopify,EXP-1" in r.text


async def test_manual_adjust_records_event_with_operator(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ADJ-1", "Adjust me")

    r = await admin_client.post(
        "/admin/adjust",
        data={
            "master_sku_id": sku_id,
            "quantity_delta": 7,
            "reason": "棚卸",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        event = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert event.event_type == InventoryEventTypeEnum.MANUAL_ADJUST
        assert event.quantity_delta == 7
        assert event.reason == "棚卸"
        assert event.operator == USER


async def test_manual_adjust_rejects_negative_stock(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "NEG-1", "Neg")

    r = await admin_client.post(
        "/admin/adjust",
        data={"master_sku_id": sku_id, "quantity_delta": -5, "reason": "x"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "insufficient" in r.headers["location"]


async def test_event_log_filters(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "EV-1", "Event")
    async with factory() as session, session.begin():
        session.add(
            InventoryEvent(
                master_sku_id=sku_id,
                event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
                quantity_delta=3,
                reason="seed",
                operator="op",
                occurred_at=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
            )
        )

    r = await admin_client.get("/admin/events?event_type=manual_adjust", headers=_auth_header())
    assert r.status_code == 200
    assert "manual_adjust" in r.text
    assert "+3" in r.text


async def test_alerts_resolve_replays_pending_order(admin_client, _test_engine) -> None:
    """End-to-end: alert resolution backfills mapping and replays parked order."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ALERT-1", "Will resolve")
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify",
                channel_sku="MISSING-1",
                status=MappingAlertStatusEnum.OPEN,
            )
        )
        order = Order(
            channel="shopify",
            channel_order_id="O-ALERT",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                line_id="L-1",
                channel_sku="MISSING-1",
                quantity=2,
                unit_price=1000,
            )
        )

    async with factory() as session:
        result = await session.execute(
            select(MappingAlert.id).where(MappingAlert.channel_sku == "MISSING-1")
        )
        alert_id = result.scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"master_sku_id": sku_id},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "resolved:1" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == MappingAlertStatusEnum.RESOLVED
        assert alert.resolved_master_sku_id == sku_id

        order = (
            await session.execute(select(Order).where(Order.channel_order_id == "O-ALERT"))
        ).scalar_one()
        assert order.status == "confirmed"

        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].quantity_delta == -2

"""Integration tests — MappingService replays pending orders on resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services import InventoryService, MappingService

pytestmark = pytest.mark.integration


async def _seed_pending_order(
    session,
    *,
    channel: str = "shopify",
    channel_order_id: str = "O-100",
    channel_sku: str = "MISSING-SKU",
    quantity: int = 2,
) -> tuple[Order, OrderItem]:
    order = Order(
        channel=channel,
        channel_order_id=channel_order_id,
        status=OrderStatusEnum.PENDING_MAPPING,
        ordered_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )
    session.add(order)
    await session.flush()
    item = OrderItem(
        order_id=order.id,
        line_id="L-1",
        channel_sku=channel_sku,
        quantity=quantity,
        unit_price=Decimal("1000.00"),
    )
    session.add(item)
    alert = MappingAlert(
        channel=channel,
        channel_sku=channel_sku,
        status=MappingAlertStatusEnum.OPEN,
    )
    session.add(alert)
    await session.flush()
    return order, item


async def test_resolve_alert_replays_pending_order(db_session) -> None:
    master = MasterSku(sku_code="MASTER-1", name="Resolved SKU")
    db_session.add(master)
    await db_session.flush()
    order, item = await _seed_pending_order(db_session)

    mapping = MappingService(db_session)
    replayed = await mapping.resolve_alert(
        channel="shopify", channel_sku="MISSING-SKU", master_sku_id=master.id
    )

    assert replayed == 1
    await db_session.refresh(order)
    await db_session.refresh(item)
    assert order.status == OrderStatusEnum.CONFIRMED
    assert item.master_sku_id == master.id

    alert = (await db_session.execute(select(MappingAlert))).scalar_one()
    assert alert.status == MappingAlertStatusEnum.RESOLVED
    assert alert.resolved_master_sku_id == master.id

    inventory = InventoryService(db_session)
    assert await inventory.get_current_stock(master.id) == -2  # oversell visible


async def test_resolve_is_safe_to_run_twice(db_session) -> None:
    """Re-running resolution must not double-decrement (idempotency invariant)."""
    master = MasterSku(sku_code="MASTER-2", name="SKU")
    db_session.add(master)
    await db_session.flush()
    await _seed_pending_order(db_session)
    mapping = MappingService(db_session)
    inventory = InventoryService(db_session)

    await mapping.resolve_alert(
        channel="shopify", channel_sku="MISSING-SKU", master_sku_id=master.id
    )
    first_stock = await inventory.get_current_stock(master.id)

    # Second invocation: nothing pending, no replay; existing event is
    # protected by the UNIQUE source constraint.
    replayed = await mapping.resolve_alert(
        channel="shopify", channel_sku="MISSING-SKU", master_sku_id=master.id
    )
    assert replayed == 0
    assert await inventory.get_current_stock(master.id) == first_stock


async def test_find_master_sku_id_returns_active_mapping(db_session) -> None:
    master = MasterSku(sku_code="MASTER-3", name="SKU")
    db_session.add(master)
    await db_session.flush()
    mapping = MappingService(db_session)
    await mapping.resolve_alert(channel="rakuten", channel_sku="RAK-X", master_sku_id=master.id)

    found = await mapping.find_master_sku_id(channel="rakuten", channel_sku="RAK-X")
    assert found == master.id

    missing = await mapping.find_master_sku_id(channel="rakuten", channel_sku="NO-SUCH")
    assert missing is None

"""Integration tests — MappingService replays pending orders on resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    BundleComponent,
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


async def test_a_partially_resolved_order_is_not_confirmed(db_session) -> None:
    """The stranding bug. A Rakuten order carrying two unknown SKUs used to go
    確定 on the first resolution, and its second line stayed NULL forever —
    never consumed, and invisible to anything that looks for pending_mapping."""
    master = MasterSku(sku_code="MASTER-PART", name="First of two")
    db_session.add(master)
    await db_session.flush()

    order, first = await _seed_pending_order(
        db_session, channel="rakuten", channel_order_id="R-PART", channel_sku="KNOWN-LATER"
    )
    second = OrderItem(
        order_id=order.id,
        line_id="L-2",
        channel_sku="STILL-UNKNOWN",
        quantity=1,
        unit_price=Decimal("500.00"),
    )
    db_session.add(second)
    db_session.add(
        MappingAlert(
            channel="rakuten",
            channel_sku="STILL-UNKNOWN",
            status=MappingAlertStatusEnum.OPEN,
        )
    )
    await db_session.flush()

    replayed = await MappingService(db_session).resolve_alert(
        channel="rakuten", channel_sku="KNOWN-LATER", master_sku_id=master.id
    )

    assert replayed == 1
    await db_session.refresh(order)
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.master_sku_id == master.id
    assert second.master_sku_id is None
    assert order.status == OrderStatusEnum.PENDING_MAPPING


async def test_resolving_the_last_line_then_confirms_the_order(db_session) -> None:
    """The other half: once nothing is unmapped, the order does settle."""
    one = MasterSku(sku_code="MASTER-L1", name="One")
    two = MasterSku(sku_code="MASTER-L2", name="Two")
    db_session.add_all([one, two])
    await db_session.flush()

    order, _ = await _seed_pending_order(
        db_session, channel="rakuten", channel_order_id="R-BOTH", channel_sku="SKU-A"
    )
    db_session.add(
        OrderItem(
            order_id=order.id,
            line_id="L-2",
            channel_sku="SKU-B",
            quantity=1,
            unit_price=Decimal("500.00"),
        )
    )
    db_session.add(
        MappingAlert(channel="rakuten", channel_sku="SKU-B", status=MappingAlertStatusEnum.OPEN)
    )
    await db_session.flush()

    service = MappingService(db_session)
    await service.resolve_alert(channel="rakuten", channel_sku="SKU-A", master_sku_id=one.id)
    await db_session.refresh(order)
    assert order.status == OrderStatusEnum.PENDING_MAPPING

    await service.resolve_alert(channel="rakuten", channel_sku="SKU-B", master_sku_id=two.id)
    await db_session.refresh(order)
    assert order.status == OrderStatusEnum.CONFIRMED


async def test_resolving_onto_a_shared_stock_parent_moves_the_components(db_session) -> None:
    """N108 42/45/53cm share one pool. Decrementing the parent leaves the pool
    untouched, so the other lengths stay advertised as available."""
    parent = MasterSku(sku_code="N108-PARENT", name="共有在庫の親", is_bundle=True)
    component = MasterSku(sku_code="N108-POOL", name="共有在庫プール")
    db_session.add_all([parent, component])
    await db_session.flush()
    db_session.add(
        BundleComponent(
            bundle_master_sku_id=parent.id,
            component_master_sku_id=component.id,
            quantity_per=1,
        )
    )
    await db_session.flush()

    await _seed_pending_order(
        db_session, channel="rakuten", channel_order_id="R-SHARED", channel_sku="N108-45"
    )
    await MappingService(db_session).resolve_alert(
        channel="rakuten", channel_sku="N108-45", master_sku_id=parent.id
    )

    inventory = InventoryService(db_session)
    assert await inventory.get_current_stock(component.id) == -2
    assert await inventory.get_current_stock(parent.id) == 0


async def test_resolving_onto_an_unmanaged_master_writes_no_event(db_session) -> None:
    """ギフトバッグ has no stock to take. Writing one here means a later
    cancellation credits stock the order never took."""
    gift = MasterSku(
        sku_code="GIFT-BAG",
        name="ギフトバッグ",
        is_stock_managed=False,
        non_inventory_kind="gift",
    )
    db_session.add(gift)
    await db_session.flush()

    await _seed_pending_order(
        db_session, channel="rakuten", channel_order_id="R-GIFT", channel_sku="GIFT-X"
    )
    replayed = await MappingService(db_session).resolve_alert(
        channel="rakuten", channel_sku="GIFT-X", master_sku_id=gift.id
    )

    assert replayed == 1
    assert await InventoryService(db_session).get_current_stock(gift.id) == 0

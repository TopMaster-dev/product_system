"""Integration tests — OrderIngestService end-to-end against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.adapters import NormalizedOrder, NormalizedOrderLine
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services import InventoryService, OrderIngestService

pytestmark = pytest.mark.integration


def _normalized(
    *,
    channel_order_id: str,
    sku: str,
    quantity: int = 1,
    channel: str = "shopify",
    status: str = "confirmed",
) -> NormalizedOrder:
    return NormalizedOrder(
        channel=channel,
        channel_order_id=channel_order_id,
        status=status,  # type: ignore[arg-type]
        ordered_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        items=[
            NormalizedOrderLine(
                line_id="L-1",
                channel_sku=sku,
                quantity=quantity,
                unit_price=Decimal("1000"),
            )
        ],
        raw_payload={"sample": True},
    )


async def _seed_mapping(session, *, channel: str, sku: str) -> int:
    master = MasterSku(sku_code=f"MASTER-{sku}", name=sku)
    session.add(master)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            master_sku_id=master.id,
            channel=channel,
            channel_sku=sku,
            is_active=True,
        )
    )
    await session.flush()
    return master.id


async def test_mapped_order_decrements_inventory(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="MAPPED")
    svc = OrderIngestService(db_session)

    result = await svc.ingest(_normalized(channel_order_id="O-1", sku="MAPPED", quantity=3))

    assert result.created
    assert result.consumed_count == 1
    assert result.pending_mapping_count == 0
    assert result.order.status == "confirmed"
    assert await InventoryService(db_session).get_current_stock(master_id) == -3


async def test_unmapped_order_parks_in_pending_mapping(db_session) -> None:
    svc = OrderIngestService(db_session)
    result = await svc.ingest(_normalized(channel_order_id="O-2", sku="UNMAPPED"))

    assert result.pending_mapping_count == 1
    assert result.consumed_count == 0
    assert result.order.status == OrderStatusEnum.PENDING_MAPPING
    item = (
        await db_session.execute(select(OrderItem).where(OrderItem.order_id == result.order.id))
    ).scalar_one()
    assert item.master_sku_id is None
    alert = (await db_session.execute(select(MappingAlert))).scalar_one()
    assert alert.status == MappingAlertStatusEnum.OPEN
    assert alert.channel_sku == "UNMAPPED"


async def test_duplicate_order_is_idempotent(db_session) -> None:
    """Re-ingestion of the same order doesn't double-decrement."""
    master_id = await _seed_mapping(db_session, channel="shopify", sku="A")
    svc = OrderIngestService(db_session)
    order = _normalized(channel_order_id="O-3", sku="A", quantity=2)

    first = await svc.ingest(order)
    second = await svc.ingest(order)

    assert first.created is True
    assert second.created is False
    assert await InventoryService(db_session).get_current_stock(master_id) == -2


async def test_cancellation_transition_compensates(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="C")
    svc = OrderIngestService(db_session)
    inventory = InventoryService(db_session)

    confirmed = _normalized(channel_order_id="O-4", sku="C", quantity=4)
    await svc.ingest(confirmed)
    assert await inventory.get_current_stock(master_id) == -4

    cancelled = _normalized(channel_order_id="O-4", sku="C", quantity=4, status="cancelled")
    result = await svc.ingest(cancelled)

    assert result.created is False
    assert result.cancelled_count == 1
    assert result.order.status == "cancelled"
    assert await inventory.get_current_stock(master_id) == 0


async def test_repeated_cancellation_does_not_double_compensate(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="D")
    svc = OrderIngestService(db_session)
    inventory = InventoryService(db_session)

    await svc.ingest(_normalized(channel_order_id="O-5", sku="D", quantity=2))
    await svc.ingest(_normalized(channel_order_id="O-5", sku="D", quantity=2, status="cancelled"))
    stock_after_first_cancel = await inventory.get_current_stock(master_id)

    # Second cancellation: order is already cancelled; no further compensation.
    result = await svc.ingest(
        _normalized(channel_order_id="O-5", sku="D", quantity=2, status="cancelled")
    )
    assert result.cancelled_count == 0
    assert await inventory.get_current_stock(master_id) == stock_after_first_cancel


async def test_cancelled_on_arrival_does_not_decrement(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="E")
    svc = OrderIngestService(db_session)

    await svc.ingest(_normalized(channel_order_id="O-6", sku="E", quantity=1, status="cancelled"))
    assert await InventoryService(db_session).get_current_stock(master_id) == 0


async def test_unmapped_alert_increments_occurrences(db_session) -> None:
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-7", sku="GHOST"))
    await svc.ingest(_normalized(channel_order_id="O-8", sku="GHOST"))

    alert = (await db_session.execute(select(MappingAlert))).scalar_one()
    assert alert.channel_sku == "GHOST"
    assert alert.occurrence_count >= 2


async def test_inventory_event_recorded_with_source(db_session) -> None:
    master_id = await _seed_mapping(db_session, channel="shopify", sku="F")
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-9", sku="F", quantity=1))

    event = (
        await db_session.execute(
            select(InventoryEvent).where(InventoryEvent.master_sku_id == master_id)
        )
    ).scalar_one()
    assert event.source_channel == "shopify"
    assert event.source_order_id == "O-9"
    assert event.source_line_id == "L-1"
    assert event.quantity_delta == -1


async def test_order_payload_persisted_as_jsonb(db_session) -> None:
    await _seed_mapping(db_session, channel="shopify", sku="G")
    svc = OrderIngestService(db_session)
    await svc.ingest(_normalized(channel_order_id="O-10", sku="G", quantity=1))

    order = (
        await db_session.execute(select(Order).where(Order.channel_order_id == "O-10"))
    ).scalar_one()
    assert order.raw_payload == {"sample": True}

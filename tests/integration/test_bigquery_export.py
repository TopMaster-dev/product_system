"""Integration tests — BigQueryExportService against in-memory BQ client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bigquery import InMemoryBigQueryClient
from app.models import (
    BigQueryExportRun,
    ChannelSkuMapping,
    InventoryEvent,
    InventoryEventTypeEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
from app.services import BigQueryExportService

pytestmark = pytest.mark.integration


async def _seed_world(session, *, ordered_at: datetime, occurred_at: datetime) -> int:
    sku = MasterSku(sku_code="BQ-1", name="BQ Test SKU")
    session.add(sku)
    await session.flush()
    session.add(
        ChannelSkuMapping(
            master_sku_id=sku.id,
            channel="shopify",
            channel_sku="BQ-CHAN-1",
            is_active=True,
        )
    )
    order = Order(
        channel="shopify",
        channel_order_id="BQ-ORD-1",
        status=OrderStatusEnum.CONFIRMED,
        ordered_at=ordered_at,
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=order.id,
            line_id="L-1",
            channel_sku="BQ-CHAN-1",
            master_sku_id=sku.id,
            quantity=2,
            unit_price=Decimal("1500.00"),
        )
    )
    session.add(
        InventoryEvent(
            master_sku_id=sku.id,
            event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
            quantity_delta=-2,
            source_channel="shopify",
            source_order_id="BQ-ORD-1",
            source_line_id="L-1",
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return sku.id


async def test_export_writes_every_table(db_session) -> None:
    seeded_at = datetime.now(UTC)
    await _seed_world(
        db_session,
        ordered_at=seeded_at - timedelta(hours=1),
        occurred_at=seeded_at - timedelta(hours=1),
    )
    client = InMemoryBigQueryClient()
    service = BigQueryExportService(db_session, client)
    now = datetime.now(UTC) + timedelta(seconds=1)

    results = await service.export_all(until=now)

    by_table = {r.table_name: r for r in results}
    assert by_table["master_skus"].rows == 1
    assert by_table["channel_sku_mappings"].rows == 1
    assert by_table["orders"].rows == 1
    assert by_table["order_items"].rows == 1
    assert by_table["inventory_events"].rows == 1
    assert by_table["inventory_snapshots"].rows == 0  # not adjusted yet
    # Every table got a run row recorded.
    rows = (await db_session.execute(select(BigQueryExportRun))).scalars().all()
    statuses = {r.table_name: r.status for r in rows}
    assert all(s == "success" for s in statuses.values())


async def test_second_run_with_same_until_is_skipped(db_session) -> None:
    seeded_at = datetime.now(UTC)
    await _seed_world(
        db_session,
        ordered_at=seeded_at - timedelta(hours=1),
        occurred_at=seeded_at - timedelta(hours=1),
    )
    client = InMemoryBigQueryClient()
    service = BigQueryExportService(db_session, client)
    now = datetime.now(UTC) + timedelta(seconds=1)

    first = await service.export_all(until=now)
    second = await service.export_all(until=now)

    assert all(r.skipped is False for r in first)
    assert all(r.skipped is True for r in second)
    # No duplicate writes to BQ for the same window.
    assert len(client.tables["master_skus"]) == 1


async def test_incremental_window_advances(db_session) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    await _seed_world(db_session, ordered_at=past, occurred_at=past)
    client = InMemoryBigQueryClient()
    service = BigQueryExportService(db_session, client)

    # First window: a fixed watermark in the past so the seeded event lands here.
    t1 = datetime.now(UTC) + timedelta(seconds=1)
    await service.export_all(until=t1)
    assert len(client.tables["inventory_events"]) == 1

    # Add a NEW event with created_at explicitly past t1.
    sku_id = (await db_session.execute(select(MasterSku.id))).scalar_one()
    future_ts = t1 + timedelta(minutes=10)
    db_session.add(
        InventoryEvent(
            master_sku_id=sku_id,
            event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
            quantity_delta=5,
            reason="incremental window test",
            operator="test",
            occurred_at=future_ts,
            created_at=future_ts,
        )
    )
    await db_session.flush()

    # Second window: until > the new event's created_at.
    t2 = future_ts + timedelta(seconds=1)
    await service.export_all(until=t2)
    assert len(client.tables["inventory_events"]) == 2


async def test_snapshot_truncates_and_reloads(db_session) -> None:
    from app.services import EventSource, InventoryService

    sku = MasterSku(sku_code="SNAP-1", name="Snapshot SKU")
    db_session.add(sku)
    await db_session.flush()
    inv = InventoryService(db_session)
    await inv.manual_adjust(master_sku_id=sku.id, quantity_delta=10, reason="seed", operator="t")

    client = InMemoryBigQueryClient()
    service = BigQueryExportService(db_session, client)
    t1 = datetime.now(UTC).replace(microsecond=0)
    await service.export_all(until=t1)
    assert len(client.tables["inventory_snapshots"]) == 1
    assert client.tables["inventory_snapshots"][0]["on_hand_qty"] == 10

    # Mutate snapshot via a consumption.
    await inv.consume_for_order_line(
        master_sku_id=sku.id,
        quantity=3,
        source=EventSource(channel="shopify", order_id="SNAP-O", line_id="L-1"),
    )
    t2 = t1 + timedelta(minutes=10)
    await service.export_all(until=t2)

    # Snapshot table should be REPLACED, not appended.
    assert len(client.tables["inventory_snapshots"]) == 1
    assert client.tables["inventory_snapshots"][0]["on_hand_qty"] == 7


async def test_failed_export_does_not_advance_watermark(db_session) -> None:
    """When BQ load fails, the next run reuses the same `since` watermark."""

    class FailingClient:
        async def load_rows(self, table, rows, *, write_mode):  # type: ignore[no-untyped-def]
            if table.name == "orders":
                raise RuntimeError("simulated BQ outage")
            return len(list(rows))

    seeded_at = datetime.now(UTC)
    await _seed_world(
        db_session,
        ordered_at=seeded_at - timedelta(hours=1),
        occurred_at=seeded_at - timedelta(hours=1),
    )
    service = BigQueryExportService(db_session, FailingClient())
    now = datetime.now(UTC) + timedelta(seconds=1)

    results = await service.export_all(until=now)
    orders_result = next(r for r in results if r.table_name == "orders")
    assert orders_result.error and "outage" in orders_result.error
    # A failed run row is recorded.
    failed = (
        await db_session.execute(
            select(BigQueryExportRun).where(
                BigQueryExportRun.table_name == "orders",
                BigQueryExportRun.status == "failed",
            )
        )
    ).scalar_one()
    assert failed.until == now
    # The successful watermark query falls back to None for orders.
    last_success = (
        await db_session.execute(
            select(BigQueryExportRun.until).where(
                BigQueryExportRun.table_name == "orders",
                BigQueryExportRun.status == "success",
            )
        )
    ).scalar_one_or_none()
    assert last_success is None  # watermark NOT advanced

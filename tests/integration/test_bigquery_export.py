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


async def _seed_watermarks(factory, until: datetime) -> None:
    """Give every incremental table a prior SUCCESSFUL run, exactly as
    production has one dated 2026-05-31. Without this the planner falls back to
    the oldest source row, whose updated_at defaults to now — which would make
    every test window collapse to one and prove nothing."""
    async with factory() as session, session.begin():
        for name in ("orders", "order_items", "inventory_events"):
            session.add(
                BigQueryExportRun(
                    table_name=name,
                    mode="incremental",
                    since=None,
                    until=until,
                    status="success",
                    row_count=0,
                    completed_at=until,
                )
            )


class _CountingClient(InMemoryBigQueryClient):
    """Counts loads per table so a test can fail a specific window."""

    def __init__(self, *, fail_table: str | None = None, fail_on_load: int = 0) -> None:
        super().__init__()
        self.loads: dict[str, int] = {}
        self._fail_table = fail_table
        self._fail_on_load = fail_on_load

    async def load_rows(self, table, rows, *, write_mode):  # type: ignore[no-untyped-def]
        self.loads[table.name] = self.loads.get(table.name, 0) + 1
        if table.name == self._fail_table and self.loads[table.name] >= self._fail_on_load:
            raise MemoryError("simulated OOM kill")
        return await super().load_rows(table, rows, write_mode=write_mode)


async def _run_with_client(factory, client, *, until, max_windows):  # type: ignore[no-untyped-def]
    import app.cli.export_to_bq as mod

    original = mod.get_bigquery_client
    mod.get_bigquery_client = lambda: client  # type: ignore[assignment]
    try:
        return await mod.run_export(factory, until=until, max_windows=max_windows)
    finally:
        mod.get_bigquery_client = original  # type: ignore[assignment]


async def test_incremental_export_is_split_into_daily_windows(_test_engine) -> None:
    """One load per day, not one load for the whole backlog — the property that
    bounds memory however far behind the export has fallen."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    t0 = datetime(2026, 5, 31, tzinfo=UTC)
    await _seed_watermarks(factory, t0)

    client = _CountingClient()
    await _run_with_client(factory, client, until=t0 + timedelta(days=5), max_windows=0)

    assert client.loads["orders"] == 5
    assert client.loads["inventory_events"] == 5
    assert client.loads["master_skus"] == 1  # snapshots are never windowed


async def test_a_kill_mid_export_keeps_every_finished_window(_test_engine) -> None:
    """The property the 2026-08-21 incident turned on.

    The old shape wrapped all six tables in ONE transaction, so a SIGKILL
    discarded every run record while BigQuery kept the rows it had accepted —
    and the next attempt re-appended them from the same unmoved watermark.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    t0 = datetime(2026, 5, 31, tzinfo=UTC)
    await _seed_watermarks(factory, t0)

    client = _CountingClient(fail_table="inventory_events", fail_on_load=3)
    results = await _run_with_client(factory, client, until=t0 + timedelta(days=5), max_windows=0)

    assert any(r.error for r in results), "the simulated kill did not surface"

    async with factory() as session:
        runs = (
            (
                await session.execute(
                    select(BigQueryExportRun).where(
                        BigQueryExportRun.table_name == "inventory_events",
                        BigQueryExportRun.status == "success",
                        BigQueryExportRun.until > t0,
                    )
                )
            )
            .scalars()
            .all()
        )
    # Windows 1 and 2 committed before window 3 died, and they stay committed.
    assert len(runs) == 2

    async with factory() as session:
        service = BigQueryExportService(session, client)
        watermark = await service.last_success_watermark("inventory_events")
    # A resumed run starts from day 2, not from the beginning — which is what
    # stops the re-append that produced duplicates in production.
    assert watermark == t0 + timedelta(days=2)


async def test_a_failed_window_stops_that_table_without_skipping_ahead(_test_engine) -> None:
    """A gap would be worse than a delay: the watermark would advance past data
    that was never exported, and nothing would ever go back for it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    t0 = datetime(2026, 5, 31, tzinfo=UTC)
    await _seed_watermarks(factory, t0)

    client = _CountingClient(fail_table="orders", fail_on_load=2)
    await _run_with_client(factory, client, until=t0 + timedelta(days=5), max_windows=0)

    # Stopped at the failure instead of grinding through windows 3-5.
    assert client.loads["orders"] == 2
    # Other tables are unaffected — one table's failure is not a global halt.
    assert client.loads["inventory_events"] == 5


async def test_capped_run_reports_the_remaining_backlog(_test_engine) -> None:
    """ "Nothing failed" and "the export is current" are different claims.
    Conflating them is what let a three-month outage look healthy."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    t0 = datetime(2026, 5, 31, tzinfo=UTC)
    await _seed_watermarks(factory, t0)

    results = await _run_with_client(
        factory, _CountingClient(), until=t0 + timedelta(days=30), max_windows=3
    )

    assert not any(r.error for r in results)
    behind = {r.table_name: r.remaining_windows for r in results if r.remaining_windows}
    assert behind == {"orders": 27, "order_items": 27, "inventory_events": 27}

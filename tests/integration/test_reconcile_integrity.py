"""Integration tests for the two W1 data-integrity fixes.

Both defects were verified in the Phase 1-B code before being fixed here:

1. `approve_diff` wrote the SCAN-TIME delta as the event while setting the
   snapshot absolutely to target_qty, so any order landing between the scan and
   the human approval left `snapshot != SUM(events)` forever.
2. `_compensate_lines` stamped the compensation at the ORIGINAL order time, so
   cancelling an old order wrote a backdated event that no forward-running
   aggregate could ever pick up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.adapters import NormalizedOrder, NormalizedOrderLine
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    ReconcileDiff,
)
from app.services import EventSource, InventoryService, OrderIngestService
from app.services.reconcile import DiffInput, ReconcileService

pytestmark = pytest.mark.integration


async def _sku(session, code: str, qty: int) -> int:
    sku = MasterSku(sku_code=code, name=code, attributes={})
    session.add(sku)
    await session.flush()
    session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=qty))
    await session.flush()
    return sku.id


async def _snapshot_and_event_sum(session, sku_id: int) -> tuple[int, int]:
    snap = (
        await session.execute(
            select(InventorySnapshot.on_hand_qty).where(InventorySnapshot.master_sku_id == sku_id)
        )
    ).scalar_one()
    total = (
        await session.execute(
            select(func.coalesce(func.sum(InventoryEvent.quantity_delta), 0)).where(
                InventoryEvent.master_sku_id == sku_id
            )
        )
    ).scalar_one()
    return snap, total


async def test_approve_diff_keeps_snapshot_equal_to_event_sum(db_session) -> None:
    """A sale lands between the reconcile scan and the operator's approval."""
    sku_id = await _sku(db_session, "DRIFT-1", 100)
    # Seed the opening balance as an event so the invariant is checkable.
    db_session.add(
        InventoryEvent(
            master_sku_id=sku_id,
            event_type="receipt",
            quantity_delta=100,
            source_channel="seed",
            source_order_id="seed",
            source_line_id="DRIFT-1",
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="test",
        triggered_by="tester",
        diffs=[DiffInput(master_sku_id=sku_id, current_qty=100, target_qty=120)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()

    # --- a sale happens AFTER the scan, BEFORE the approval ---
    await InventoryService(db_session).consume_for_order_line(
        master_sku_id=sku_id,
        quantity=5,
        source=EventSource(channel="shopify", order_id="O-MID", line_id="L1"),
    )
    await db_session.flush()

    await svc.approve_diff(run_id=run.id, diff_id=diff_id, approved_by="tester")
    await db_session.flush()

    snap, total = await _snapshot_and_event_sum(db_session, sku_id)
    assert snap == 120, "stocktake target must win"
    assert snap == total, (
        f"snapshot ({snap}) must equal SUM(events) ({total}); the approved delta "
        "has to be recomputed from the CURRENT quantity, not the scan-time one"
    )


async def test_cancellation_is_stamped_at_cancellation_time(db_session) -> None:
    """A 60-day-old order cancelled today must NOT write a 60-day-old event."""
    sku_id = await _sku(db_session, "CANCEL-1", 50)
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=sku_id, channel="shopify", channel_sku="CANCEL-1", is_active=True
        )
    )
    await db_session.flush()

    ordered_at = datetime.now(UTC) - timedelta(days=60)
    ingest = OrderIngestService(db_session)
    payload = NormalizedOrder(
        channel="shopify",
        channel_order_id="O-OLD",
        status="confirmed",
        ordered_at=ordered_at,
        items=[
            NormalizedOrderLine(line_id="L1", channel_sku="CANCEL-1", quantity=2, unit_price=1000)
        ],
    )
    await ingest.ingest(payload)
    await db_session.flush()

    await ingest.ingest(payload.model_copy(update={"status": "cancelled"}))
    await db_session.flush()

    ev = (
        await db_session.execute(
            select(InventoryEvent).where(
                InventoryEvent.master_sku_id == sku_id,
                InventoryEvent.event_type == "cancellation_returned",
            )
        )
    ).scalar_one()
    assert ev.occurred_at > ordered_at + timedelta(days=1), (
        "the compensation must be stamped at cancellation time; backdating it to "
        "the order date corrupts every daily aggregate from that date forward"
    )
    assert (datetime.now(UTC) - ev.occurred_at) < timedelta(minutes=5)


async def test_unmanaged_sku_produces_no_inventory_events(db_session) -> None:
    """A non-stock-managed SKU must not consume OR compensate.

    Guarding only the consume path would let a later cancellation ADD phantom
    stock, which is why the check lives in resolve_consumption().
    """
    from app.models import ChannelSkuMapping

    sku = MasterSku(
        sku_code="GIFTBOX",
        name="ギフトボックス",
        attributes={},
        is_stock_managed=False,
        non_inventory_kind="packaging",
    )
    db_session.add(sku)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=sku.id, channel="shopify", channel_sku="GIFTBOX", is_active=True
        )
    )
    await db_session.flush()

    assert await InventoryService(db_session).resolve_consumption(sku.id) == []

    ingest = OrderIngestService(db_session)
    payload = NormalizedOrder(
        channel="shopify",
        channel_order_id="O-GIFT",
        status="confirmed",
        ordered_at=datetime.now(UTC),
        items=[NormalizedOrderLine(line_id="L1", channel_sku="GIFTBOX", quantity=3, unit_price=0)],
    )
    await ingest.ingest(payload)
    await db_session.flush()
    # ... and the cancellation must not add stock back either.
    await ingest.ingest(payload.model_copy(update={"status": "cancelled"}))
    await db_session.flush()

    events = (
        (
            await db_session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku.id)
            )
        )
        .scalars()
        .all()
    )
    assert events == [], "a non-stock-managed SKU must never produce inventory events"


async def test_managed_sku_still_consumes(db_session) -> None:
    """Regression guard: the new check must not disable normal SKUs."""
    sku_id = await _sku(db_session, "NORMAL-1", 10)
    assert await InventoryService(db_session).resolve_consumption(sku_id) == [(sku_id, 1)]


async def test_approve_diff_rejects_mismatched_run_id(db_session) -> None:
    """A wrong run_id must fail loudly, not lock the wrong row."""
    sku_id = await _sku(db_session, "MISMATCH-1", 10)
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="test",
        triggered_by="tester",
        diffs=[DiffInput(master_sku_id=sku_id, current_qty=10, target_qty=12)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()

    with pytest.raises(ValueError, match="belongs to run"):
        await svc.approve_diff(run_id=run.id + 999, diff_id=diff_id, approved_by="tester")


async def test_approve_diff_records_applied_delta(db_session) -> None:
    """The audit trail must show what was actually written, not the scan value."""
    sku_id = await _sku(db_session, "APPLIED-1", 100)
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="test",
        triggered_by="tester",
        diffs=[DiffInput(master_sku_id=sku_id, current_qty=100, target_qty=120)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()

    await InventoryService(db_session).consume_for_order_line(
        master_sku_id=sku_id,
        quantity=5,
        source=EventSource(channel="shopify", order_id="O-APPLIED", line_id="L1"),
    )
    await db_session.flush()

    result = await svc.approve_diff(run_id=run.id, diff_id=diff_id, approved_by="tester")
    assert result.diff.delta == 20, "scan-time proposal is preserved"
    assert result.diff.applied_delta == 25, "actually applied 120 - 95"
    assert result.event is not None and result.event.quantity_delta == 25

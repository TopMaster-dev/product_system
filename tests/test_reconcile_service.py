"""Unit tests for ReconcileService (Phase 1-B F1.7).

These tests are *partially* in-memory: they use the real Postgres test DB
when available (via the conftest fixtures) because the service's locking
semantics (SELECT FOR UPDATE) and FK behavior matter to its correctness.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
    ReconcileDiff,
    ReconcileDiffDecisionEnum,
    ReconcileRun,
    ReconcileRunStatusEnum,
)
from app.services.reconcile import DiffInput, ReconcileService

pytestmark = pytest.mark.integration


# ---------- fixtures ----------


async def _make_master(session: AsyncSession, sku_code: str = "TEST-1") -> MasterSku:
    sku = MasterSku(sku_code=sku_code, name=f"name-{sku_code}", attributes={})
    session.add(sku)
    await session.flush()
    return sku


async def _make_snapshot(
    session: AsyncSession, master_sku_id: int, on_hand: int
) -> InventorySnapshot:
    snap = InventorySnapshot(master_sku_id=master_sku_id, on_hand_qty=on_hand)
    session.add(snap)
    await session.flush()
    return snap


# ---------- start_run ----------


@pytest.mark.asyncio
async def test_start_run_persists_only_actual_diffs(db_session: AsyncSession) -> None:
    sku_a = await _make_master(db_session, "A")
    sku_b = await _make_master(db_session, "B")
    sku_c = await _make_master(db_session, "C")

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="cross_mall_csv",
        triggered_by="cloud_scheduler",
        diffs=[
            DiffInput(master_sku_id=sku_a.id, current_qty=10, target_qty=12),  # diff
            DiffInput(master_sku_id=sku_b.id, current_qty=5, target_qty=5),  # no-op
            DiffInput(master_sku_id=sku_c.id, current_qty=0, target_qty=-2),  # neg delta
        ],
    )

    assert run.diff_count == 2
    assert run.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value

    rows = (
        (
            await db_session.execute(
                select(ReconcileDiff).where(ReconcileDiff.reconcile_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    rows_by_sku = {r.master_sku_id: r for r in rows}
    assert sku_b.id not in rows_by_sku  # no-op skipped
    assert rows_by_sku[sku_a.id].delta == 2
    assert rows_by_sku[sku_c.id].delta == -2


@pytest.mark.asyncio
async def test_start_run_with_no_diffs_finalizes_immediately(
    db_session: AsyncSession,
) -> None:
    sku = await _make_master(db_session, "X")
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="cross_mall_csv",
        triggered_by="op-1",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=7, target_qty=7)],
    )
    assert run.diff_count == 0
    assert run.status == ReconcileRunStatusEnum.APPLIED.value
    assert run.finished_at is not None


# ---------- approve_diff ----------


@pytest.mark.asyncio
async def test_approve_diff_creates_stocktake_event_and_updates_snapshot(
    db_session: AsyncSession,
) -> None:
    sku = await _make_master(db_session, "S1")
    await _make_snapshot(db_session, sku.id, on_hand=20)

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="cross_mall_csv",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=20, target_qty=30)],
    )
    diff_row = (
        await db_session.execute(
            select(ReconcileDiff).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()
    result = await svc.approve_diff(diff_id=diff_row.id, approved_by="馬渡")

    assert result.event is not None
    assert result.event.event_type == InventoryEventTypeEnum.STOCKTAKE
    assert result.event.quantity_delta == 10
    assert result.event.operator == "馬渡"

    snap = (
        await db_session.execute(
            select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku.id)
        )
    ).scalar_one()
    assert snap.on_hand_qty == 30

    # Run counter incremented
    run = (
        await db_session.execute(select(ReconcileRun).where(ReconcileRun.id == run.id))
    ).scalar_one()
    assert run.applied_count == 1

    # Diff carries the new event id
    diff_after = (
        await db_session.execute(select(ReconcileDiff).where(ReconcileDiff.id == diff_row.id))
    ).scalar_one()
    assert diff_after.decision == ReconcileDiffDecisionEnum.APPROVED.value
    assert diff_after.applied_event_id == result.event.id


@pytest.mark.asyncio
async def test_approve_diff_is_idempotent(db_session: AsyncSession) -> None:
    sku = await _make_master(db_session, "I")
    await _make_snapshot(db_session, sku.id, on_hand=5)

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=5, target_qty=10)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()

    first = await svc.approve_diff(diff_id=diff_id, approved_by="a")
    second = await svc.approve_diff(diff_id=diff_id, approved_by="b")

    assert first.event is not None
    assert second.event is not None
    assert first.event.id == second.event.id
    # Snapshot still 10, not 15
    snap = (
        await db_session.execute(
            select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku.id)
        )
    ).scalar_one()
    assert snap.on_hand_qty == 10


@pytest.mark.asyncio
async def test_approve_diff_rejects_skipped(db_session: AsyncSession) -> None:
    sku = await _make_master(db_session, "K")
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=5, target_qty=8)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()
    await svc.skip_diff(diff_id=diff_id, approved_by="a")

    with pytest.raises(RuntimeError, match="skipped"):
        await svc.approve_diff(diff_id=diff_id, approved_by="b")


# ---------- skip_diff ----------


@pytest.mark.asyncio
async def test_skip_diff_records_decision_without_event(
    db_session: AsyncSession,
) -> None:
    sku = await _make_master(db_session, "SK")
    await _make_snapshot(db_session, sku.id, on_hand=42)

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=42, target_qty=100)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()
    diff = await svc.skip_diff(diff_id=diff_id, approved_by="op")

    assert diff.decision == ReconcileDiffDecisionEnum.SKIPPED.value
    snap = (
        await db_session.execute(
            select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku.id)
        )
    ).scalar_one()
    assert snap.on_hand_qty == 42  # unchanged
    events = (
        (
            await db_session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku.id)
            )
        )
        .scalars()
        .all()
    )
    assert events == []


@pytest.mark.asyncio
async def test_skip_diff_rejects_approved(db_session: AsyncSession) -> None:
    sku = await _make_master(db_session, "SK2")
    await _make_snapshot(db_session, sku.id, on_hand=10)
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=10, target_qty=15)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()
    await svc.approve_diff(diff_id=diff_id, approved_by="op")

    with pytest.raises(RuntimeError, match="already approved"):
        await svc.skip_diff(diff_id=diff_id, approved_by="op")


# ---------- finalize / cancel ----------


@pytest.mark.asyncio
async def test_finalize_run_requires_all_diffs_decided(db_session: AsyncSession) -> None:
    sku1 = await _make_master(db_session, "F1")
    sku2 = await _make_master(db_session, "F2")
    await _make_snapshot(db_session, sku1.id, on_hand=10)
    await _make_snapshot(db_session, sku2.id, on_hand=20)

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[
            DiffInput(master_sku_id=sku1.id, current_qty=10, target_qty=12),
            DiffInput(master_sku_id=sku2.id, current_qty=20, target_qty=22),
        ],
    )

    with pytest.raises(RuntimeError, match="pending"):
        await svc.finalize_run(run_id=run.id, approved_by="op")

    # Decide both, then finalize
    diff_ids = list(
        (
            await db_session.execute(
                select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    await svc.approve_diff(diff_id=diff_ids[0], approved_by="op")
    await svc.skip_diff(diff_id=diff_ids[1], approved_by="op")

    finalized = await svc.finalize_run(run_id=run.id, approved_by="op")
    assert finalized.status == ReconcileRunStatusEnum.APPLIED.value
    assert finalized.approved_by == "op"
    assert finalized.approved_at is not None


@pytest.mark.asyncio
async def test_cancel_run_blocked_after_first_approval(db_session: AsyncSession) -> None:
    sku = await _make_master(db_session, "CN")
    await _make_snapshot(db_session, sku.id, on_hand=5)
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=5, target_qty=6)],
    )
    diff_id = (
        await db_session.execute(
            select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run.id)
        )
    ).scalar_one()
    await svc.approve_diff(diff_id=diff_id, approved_by="op")
    with pytest.raises(RuntimeError, match="approved diffs"):
        await svc.cancel_run(run_id=run.id, cancelled_by="op")


@pytest.mark.asyncio
async def test_cancel_run_allowed_when_no_diffs_approved(
    db_session: AsyncSession,
) -> None:
    sku = await _make_master(db_session, "CN2")
    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[DiffInput(master_sku_id=sku.id, current_qty=5, target_qty=9)],
    )
    cancelled = await svc.cancel_run(run_id=run.id, cancelled_by="op", reason="invalid CSV")
    assert cancelled.status == ReconcileRunStatusEnum.CANCELLED.value
    assert cancelled.notes == "invalid CSV"


# ---------- list_approved_diffs ----------


@pytest.mark.asyncio
async def test_list_approved_diffs_returns_only_approved(
    db_session: AsyncSession,
) -> None:
    sku_a = await _make_master(db_session, "LA")
    sku_b = await _make_master(db_session, "LB")
    sku_c = await _make_master(db_session, "LC")
    for s in (sku_a, sku_b, sku_c):
        await _make_snapshot(db_session, s.id, on_hand=0)

    svc = ReconcileService(db_session)
    run = await svc.start_run(
        source="m",
        triggered_by="op",
        diffs=[
            DiffInput(master_sku_id=sku_a.id, current_qty=0, target_qty=5),
            DiffInput(master_sku_id=sku_b.id, current_qty=0, target_qty=10),
            DiffInput(master_sku_id=sku_c.id, current_qty=0, target_qty=15),
        ],
    )
    diff_rows = (
        (
            await db_session.execute(
                select(ReconcileDiff)
                .where(ReconcileDiff.reconcile_run_id == run.id)
                .order_by(ReconcileDiff.master_sku_id)
            )
        )
        .scalars()
        .all()
    )
    await svc.approve_diff(diff_id=diff_rows[0].id, approved_by="op")
    await svc.skip_diff(diff_id=diff_rows[1].id, approved_by="op")
    await svc.approve_diff(diff_id=diff_rows[2].id, approved_by="op")

    approved = await svc.list_approved_diffs(run.id)
    approved_skus = {d.master_sku_id for d in approved}
    assert approved_skus == {sku_a.id, sku_c.id}

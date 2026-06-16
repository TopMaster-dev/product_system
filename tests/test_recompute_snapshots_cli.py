"""Tests for app.cli.recompute_snapshots (projection rebuild / drift check)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli.recompute_snapshots import parse_args, run
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku


def _factory_for_engine(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


# ---------- argparse (unit) ----------


@pytest.mark.unit
def test_parse_args_master_sku_id_repeatable() -> None:
    args = parse_args(["--master-sku-id", "1", "--master-sku-id", "2"])
    assert args.master_sku_ids == [1, 2]
    assert args.all_skus is False


@pytest.mark.unit
def test_parse_args_all() -> None:
    args = parse_args(["--all"])
    assert args.all_skus is True


@pytest.mark.unit
def test_parse_args_requires_all_or_ids() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--dry-run"])  # neither --all nor --master-sku-id


# ---------- behavior (integration) ----------


async def _seed_sku_with_events(
    factory: async_sessionmaker[AsyncSession],
    *,
    sku_code: str,
    deltas: list[int],
    snapshot_qty: int | None,
) -> int:
    async with factory() as setup, setup.begin():
        sku = MasterSku(sku_code=sku_code, name=sku_code, attributes={})
        setup.add(sku)
        await setup.flush()
        now = datetime.now(UTC)
        for d in deltas:
            setup.add(
                InventoryEvent(
                    master_sku_id=sku.id,
                    event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
                    quantity_delta=d,
                    reason="seed",
                    operator="t",
                    occurred_at=now,
                )
            )
        if snapshot_qty is not None:
            setup.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=snapshot_qty))
        return sku.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_fixes_drift(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    # events sum to 8, but the snapshot is deliberately wrong (99)
    sku_id = await _seed_sku_with_events(factory, sku_code="RC-A", deltas=[10, -2], snapshot_qty=99)

    code = await run(master_sku_ids=[sku_id], all_skus=False, session_factory=factory)
    assert code == 0

    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 8
        max_event_id = (
            (
                await verify.execute(
                    select(InventoryEvent.id)
                    .where(InventoryEvent.master_sku_id == sku_id)
                    .order_by(InventoryEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert snap.last_event_id == max_event_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_dry_run_writes_nothing(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    sku_id = await _seed_sku_with_events(factory, sku_code="RC-B", deltas=[5], snapshot_qty=42)

    code = await run(master_sku_ids=[sku_id], all_skus=False, dry_run=True, session_factory=factory)
    assert code == 0
    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 42  # unchanged in dry-run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recompute_no_drift_is_noop(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    # snapshot already equals the event sum (7); no update expected
    sku_id = await _seed_sku_with_events(factory, sku_code="RC-C", deltas=[3, 4], snapshot_qty=7)
    # also align last_event_id so there is truly no drift
    async with factory() as fix, fix.begin():
        max_id = (
            (
                await fix.execute(
                    select(InventoryEvent.id)
                    .where(InventoryEvent.master_sku_id == sku_id)
                    .order_by(InventoryEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
        snap = (
            await fix.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        snap.last_event_id = max_id

    code = await run(master_sku_ids=[sku_id], all_skus=False, session_factory=factory)
    assert code == 0
    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 7

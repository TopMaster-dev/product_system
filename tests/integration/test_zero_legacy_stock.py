"""Integration tests — zero_legacy_stock scoping, zeroing, and idempotency."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.zero_legacy_stock import legacy_master_ids, run
from app.models import ChannelSkuMapping, InventoryEvent, InventorySnapshot, MasterSku

pytestmark = pytest.mark.integration


async def test_zeroes_only_migrated_legacy_snapshots(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        old = MasterSku(sku_code="006c", name="old", attributes={})  # migrated legacy product
        variant = MasterSku(
            sku_code="N23gold", name="v", attributes={"token": "N23", "color": "gold", "size": ""}
        )
        unmigrated = MasterSku(sku_code="999c", name="u", attributes={})  # not migrated
        session.add_all([old, variant, unmigrated])
        await session.flush()
        old_id, variant_id, unmig_id = old.id, variant.id, unmigrated.id
        session.add_all(
            [
                ChannelSkuMapping(
                    master_sku_id=variant.id,
                    channel="crossmall",
                    channel_sku="006c|gold|",
                    is_active=True,
                ),
                InventorySnapshot(master_sku_id=old.id, on_hand_qty=-5),
                InventorySnapshot(master_sku_id=variant.id, on_hand_qty=27),
                InventorySnapshot(master_sku_id=unmigrated.id, on_hand_qty=-3),
            ]
        )

    # Only the migrated legacy product (006c) is in scope.
    async with factory() as session:
        assert set(await legacy_master_ids(session)) == {old_id}

    # Dry-run changes nothing.
    await run(dry_run=True, session_factory=factory)
    async with factory() as session:
        snap = (
            await session.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == old_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == -5

    # Real run zeroes only the legacy snapshot; variant + unmigrated untouched.
    await run(dry_run=False, session_factory=factory)
    async with factory() as session:
        assert (
            await session.execute(
                select(InventorySnapshot.on_hand_qty).where(
                    InventorySnapshot.master_sku_id == old_id
                )
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                select(InventorySnapshot.on_hand_qty).where(
                    InventorySnapshot.master_sku_id == variant_id
                )
            )
        ).scalar_one() == 27
        assert (
            await session.execute(
                select(InventorySnapshot.on_hand_qty).where(
                    InventorySnapshot.master_sku_id == unmig_id
                )
            )
        ).scalar_one() == -3
        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(
                        InventoryEvent.master_sku_id == old_id,
                        InventoryEvent.event_type == "stocktake",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].quantity_delta == 5  # -5 -> 0

    # Idempotent: re-running adds no further events.
    await run(dry_run=False, session_factory=factory)
    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(
                        InventoryEvent.master_sku_id == old_id,
                        InventoryEvent.event_type == "stocktake",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

"""Integration tests — archive_legacy_skus: scope, the three guards, rollback."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.archive_legacy_skus import run
from app.models import (
    BundleComponent,
    ChannelSkuMapping,
    InventorySnapshot,
    MasterSku,
)

pytestmark = pytest.mark.integration


async def _seed_cutover(factory) -> dict[str, int]:
    """A miniature post-cutover world: one variant master carrying the crossmall
    mapping, plus legacy product-level masters in every interesting state."""
    async with factory() as session, session.begin():
        variant = MasterSku(
            sku_code="N23gold",
            name="variant",
            attributes={"token": "N23", "color": "gold", "size": ""},
        )
        inert = MasterSku(sku_code="006c", name="legacy inert", attributes={})
        with_stock = MasterSku(sku_code="007c", name="legacy w/ stock", attributes={})
        with_mapping = MasterSku(sku_code="008c", name="legacy w/ mapping", attributes={})
        component = MasterSku(sku_code="009c", name="legacy component", attributes={})
        live_set = MasterSku(sku_code="SET-1", name="live set", is_bundle=True, attributes={})
        session.add_all([variant, inert, with_stock, with_mapping, component, live_set])
        await session.flush()

        session.add_all(
            [
                # These crossmall mappings are what puts 006c..009c "in scope":
                # their 商品コード really migrated to a variant master.
                ChannelSkuMapping(
                    master_sku_id=variant.id,
                    channel="crossmall",
                    channel_sku="006c|gold|",
                    is_active=True,
                ),
                ChannelSkuMapping(
                    master_sku_id=variant.id,
                    channel="crossmall",
                    channel_sku="007c|gold|",
                    is_active=True,
                ),
                ChannelSkuMapping(
                    master_sku_id=variant.id,
                    channel="crossmall",
                    channel_sku="008c|gold|",
                    is_active=True,
                ),
                ChannelSkuMapping(
                    master_sku_id=variant.id,
                    channel="crossmall",
                    channel_sku="009c|gold|",
                    is_active=True,
                ),
                # Guard 1: physical stock still on hand.
                InventorySnapshot(master_sku_id=with_stock.id, on_hand_qty=6),
                InventorySnapshot(master_sku_id=inert.id, on_hand_qty=0),
                # Guard 2: a channel can still order it.
                ChannelSkuMapping(
                    master_sku_id=with_mapping.id,
                    channel="shopify",
                    channel_sku="008c",
                    is_active=True,
                ),
                # Guard 3: a live set still fans out to it.
                BundleComponent(
                    bundle_master_sku_id=live_set.id,
                    component_master_sku_id=component.id,
                    quantity_per=1,
                ),
            ]
        )
        return {
            "variant": variant.id,
            "inert": inert.id,
            "with_stock": with_stock.id,
            "with_mapping": with_mapping.id,
            "component": component.id,
            "live_set": live_set.id,
        }


async def _archived(factory, master_id: int) -> bool:
    async with factory() as session:
        master = (
            await session.execute(select(MasterSku).where(MasterSku.id == master_id))
        ).scalar_one()
        return master.archived_at is not None


async def test_archives_only_the_inert_legacy_masters(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    assert await run(dry_run=False, session_factory=factory) == 0

    assert await _archived(factory, ids["inert"]) is True
    # The three guards each refuse: archiving a live SKU hides it from the
    # operator while orders keep consuming it.
    assert await _archived(factory, ids["with_stock"]) is False
    assert await _archived(factory, ids["with_mapping"]) is False
    assert await _archived(factory, ids["component"]) is False
    # Variant masters are the cutover TARGET, never in scope.
    assert await _archived(factory, ids["variant"]) is False


async def test_dry_run_changes_nothing(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    await run(dry_run=True, session_factory=factory)

    assert await _archived(factory, ids["inert"]) is False


async def test_unarchive_rolls_the_whole_scope_back(_test_engine) -> None:
    """The client signs off on a list of ~400 masters; if they object afterwards
    the change has to come back off without a restore."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    await run(dry_run=False, session_factory=factory)
    assert await _archived(factory, ids["inert"]) is True

    await run(dry_run=False, unarchive=True, session_factory=factory)
    async with factory() as session:
        master = (
            await session.execute(select(MasterSku).where(MasterSku.id == ids["inert"]))
        ).scalar_one()
    assert master.archived_at is None
    assert master.archived_reason is None


async def test_rerun_is_idempotent(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    await run(dry_run=False, reason="first", session_factory=factory)
    async with factory() as session:
        stamp = (
            (await session.execute(select(MasterSku).where(MasterSku.id == ids["inert"])))
            .scalar_one()
            .archived_at
        )

    await run(dry_run=False, reason="second", session_factory=factory)
    async with factory() as session:
        master = (
            await session.execute(select(MasterSku).where(MasterSku.id == ids["inert"]))
        ).scalar_one()
    # Already archived: neither the timestamp nor the reason is rewritten.
    assert master.archived_at == stamp
    assert master.archived_reason == "first"


async def test_component_of_an_archived_set_is_no_longer_blocked(_test_engine) -> None:
    """Guard 3 asks whether the PARENT is still live, so archiving the set first
    releases its components on the next run — the cleanup can converge."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    async with factory() as session, session.begin():
        parent = (
            await session.execute(select(MasterSku).where(MasterSku.id == ids["live_set"]))
        ).scalar_one()
        parent.archived_at = datetime.now(UTC)

    await run(dry_run=False, session_factory=factory)
    assert await _archived(factory, ids["component"]) is True


async def test_empty_scope_is_not_an_error(_test_engine) -> None:
    """A fresh database has no crossmall mappings at all."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    assert await run(dry_run=False, session_factory=factory) == 0


async def test_ui_and_cli_ask_the_same_question(_test_engine) -> None:
    """`archive_blockers` is shared by the bulk CLI and the per-row admin toggle.
    Two implementations of "is this SKU still in use?" would eventually disagree,
    and the one answering "no" wins by hiding a live SKU from every screen."""
    from app.services.sku_scope import archive_blockers

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ids = await _seed_cutover(factory)

    async with factory() as session:
        blockers = await archive_blockers(session, list(ids.values()))

    assert ids["with_stock"] in blockers.with_stock
    assert ids["with_mapping"] in blockers.with_active_mapping
    assert ids["component"] in blockers.component_of_live_bundle
    assert ids["inert"] not in blockers.blocked()
    # Every reason is reported, not just the first one found.
    assert blockers.reasons_for(ids["with_stock"]) == ["在庫が残っています"]
    assert blockers.reasons_for(ids["inert"]) == []


async def test_archive_blockers_on_an_empty_list_hits_no_database(_test_engine) -> None:
    from app.services.sku_scope import archive_blockers

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        blockers = await archive_blockers(session, [])
    assert blockers.blocked() == set()

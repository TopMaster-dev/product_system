"""Tests for app.cli.adjust_inventory (forward-fix manual adjustment)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli.adjust_inventory import parse_args, run
from app.models import InventoryEvent, InventorySnapshot, MasterSku


def _factory_for_engine(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


# ---------- argparse (unit) ----------


@pytest.mark.unit
def test_parse_args_basic() -> None:
    args = parse_args(
        ["--master-sku-id", "42", "--delta", "-1", "--reason", "r", "--operator", "op"]
    )
    assert args.master_sku_id == 42
    assert args.delta == -1
    assert args.reason == "r"
    assert args.operator == "op"
    assert args.dry_run is False


@pytest.mark.unit
def test_parse_args_requires_all_flags() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--master-sku-id", "1", "--delta", "1"])  # missing reason/operator


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_delta_is_usage_error() -> None:
    # delta==0 is rejected before any DB access, so no factory needed.
    code = await run(master_sku_id=1, delta=0, reason="r", operator="op")
    assert code == 2


# ---------- behavior (integration) ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_appends_compensating_event(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        sku = MasterSku(sku_code="ADJ-A", name="A", attributes={})
        setup.add(sku)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=10))
        sku_id = sku.id

    code = await run(
        master_sku_id=sku_id,
        delta=-3,
        reason="F1.7 rollback",
        operator="verifier",
        session_factory=factory,
    )
    assert code == 0

    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 7
        events = (
            (
                await verify.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].event_type == "manual_adjust"
        assert events[0].quantity_delta == -3
        assert snap.last_event_id == events[0].id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_rejects_negative_result(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        sku = MasterSku(sku_code="ADJ-B", name="B", attributes={})
        setup.add(sku)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=2))
        sku_id = sku.id

    code = await run(
        master_sku_id=sku_id,
        delta=-5,  # would leave -3
        reason="bad",
        operator="verifier",
        session_factory=factory,
    )
    assert code == 1
    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 2  # unchanged
        events = (
            (
                await verify.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert events == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_dry_run_writes_nothing(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        sku = MasterSku(sku_code="ADJ-C", name="C", attributes={})
        setup.add(sku)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=5))
        sku_id = sku.id

    code = await run(
        master_sku_id=sku_id,
        delta=-1,
        reason="dry",
        operator="verifier",
        dry_run=True,
        session_factory=factory,
    )
    assert code == 0
    async with factory() as verify:
        snap = (
            await verify.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 5
        events = (
            (
                await verify.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert events == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_unknown_sku_fails(_test_engine: AsyncEngine) -> None:
    factory = _factory_for_engine(_test_engine)
    code = await run(
        master_sku_id=999999,
        delta=1,
        reason="r",
        operator="op",
        session_factory=factory,
    )
    assert code == 1


@pytest.mark.unit
def test_parse_args_accepts_sku_code_instead_of_id() -> None:
    args = parse_args(["--sku-code", "B09", "--delta", "-2", "--reason", "r", "--operator", "op"])
    assert args.sku_code == "B09"
    assert args.master_sku_id is None


@pytest.mark.unit
def test_parse_args_requires_exactly_one_target() -> None:
    """Mutually exclusive and required: passing neither, or both, is a usage
    error rather than a silent precedence rule nobody can remember."""
    with pytest.raises(SystemExit):
        parse_args(["--delta", "-1", "--reason", "r", "--operator", "op"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--master-sku-id",
                "1",
                "--sku-code",
                "B09",
                "--delta",
                "-1",
                "--reason",
                "r",
                "--operator",
                "op",
            ]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_by_sku_code_resolves_the_right_master(_test_engine: AsyncEngine) -> None:
    """Legacy masters sit alongside variants with confusingly similar names, so
    resolving `B09` must not touch `B09gold`."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import InventorySnapshot, MasterSku
    from app.services.inventory import InventoryService

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        legacy = MasterSku(sku_code="SKUCODE-B09", name="legacy")
        variant = MasterSku(sku_code="SKUCODE-B09gold", name="variant")
        session.add_all([legacy, variant])
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=legacy.id, on_hand_qty=2))
        session.add(InventorySnapshot(master_sku_id=variant.id, on_hand_qty=40))
        legacy_id, variant_id = legacy.id, variant.id

    code = await run(
        sku_code="SKUCODE-B09",
        delta=-2,
        reason="cutover後のキャンセル戻りによる幽霊在庫",
        operator="admin",
        session_factory=factory,
    )
    assert code == 0

    async with factory() as session:
        svc = InventoryService(session)
        assert await svc.get_current_stock(legacy_id) == 0
        assert await svc.get_current_stock(variant_id) == 40  # untouched


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adjust_by_unknown_sku_code_fails_clearly(_test_engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    code = await run(
        sku_code="NO-SUCH-SKU-CODE",
        delta=-1,
        reason="r",
        operator="op",
        session_factory=factory,
    )
    assert code == 1

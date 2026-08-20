"""Integration tests — mark_non_inventory: flagging, zeroing, idempotency."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.mark_non_inventory import run
from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)

pytestmark = pytest.mark.integration


def _csv(tmp_path: pathlib.Path, body: str, encoding: str = "utf-8-sig") -> pathlib.Path:
    path = tmp_path / "non_inventory.csv"
    path.write_bytes(body.encode(encoding))
    return path


async def _seed(factory, code: str, qty: int | None) -> int:
    """`qty=None` seeds a master with no snapshot row at all."""
    async with factory() as session, session.begin():
        master = MasterSku(sku_code=code, name=code, attributes={})
        session.add(master)
        await session.flush()
        if qty is not None:
            session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=qty))
        return master.id


async def _master(factory, master_id: int) -> MasterSku:
    async with factory() as session:
        return (
            await session.execute(select(MasterSku).where(MasterSku.id == master_id))
        ).scalar_one()


async def _qty(factory, master_id: int) -> int | None:
    async with factory() as session:
        return await session.scalar(
            select(InventorySnapshot.on_hand_qty).where(
                InventorySnapshot.master_sku_id == master_id
            )
        )


async def test_flags_and_zeroes_the_accumulated_negative(_test_engine, tmp_path) -> None:
    """The two halves must happen together: flagging alone leaves -12509 sitting
    on the screen, zeroing alone lets the next order drive it negative again."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    box = await _seed(factory, "H1", -12509)

    csv = _csv(tmp_path, "sku_code,kind\nH1,packaging\n")
    assert await run(csv_path=csv, dry_run=False, session_factory=factory) == 0

    master = await _master(factory, box)
    assert master.is_stock_managed is False
    assert master.non_inventory_kind == "packaging"
    assert await _qty(factory, box) == 0


async def test_dry_run_changes_nothing(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    box = await _seed(factory, "H2", -40)

    await run(
        csv_path=_csv(tmp_path, "sku_code,kind\nH2,coupon\n"), dry_run=True, session_factory=factory
    )

    assert (await _master(factory, box)).is_stock_managed is True
    assert await _qty(factory, box) == -40


async def test_second_run_does_not_correct_twice(_test_engine, tmp_path) -> None:
    """The stocktake carries a FIXED source key, so a repeat run collides with
    the UNIQUE constraint and skips instead of applying a second correction."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    box = await _seed(factory, "H3", -7)
    csv = _csv(tmp_path, "sku_code,kind\nH3,packaging\n")

    await run(csv_path=csv, dry_run=False, session_factory=factory)
    await run(csv_path=csv, dry_run=False, session_factory=factory)

    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(
                        InventoryEvent.master_sku_id == box,
                        InventoryEvent.event_type == InventoryEventTypeEnum.STOCKTAKE,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert await _qty(factory, box) == 0


async def test_unmark_restores_the_flag_without_resurrecting_the_phantom(
    _test_engine, tmp_path
) -> None:
    """Reversing a mistaken flag must not restore the negative — the zeroing was
    a correction of bad data, not a side effect to undo."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    box = await _seed(factory, "H4", -12)
    csv = _csv(tmp_path, "sku_code,kind\nH4,packaging\n")

    await run(csv_path=csv, dry_run=False, session_factory=factory)
    await run(csv_path=csv, dry_run=False, unmark=True, session_factory=factory)

    master = await _master(factory, box)
    assert master.is_stock_managed is True
    assert master.non_inventory_kind is None  # CHECK constraint keeps the pair consistent
    assert await _qty(factory, box) == 0


async def test_unknown_sku_codes_are_reported_not_fatal(_test_engine, tmp_path) -> None:
    """CROSS MALL-era codes in the client's sheet must not abort the whole run —
    but the exit code has to be non-zero so a scripted run notices."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    known = await _seed(factory, "H5", 0)
    csv = _csv(tmp_path, "sku_code,kind\nH5,packaging\nNOPE,coupon\n", encoding="cp932")

    assert await run(csv_path=csv, dry_run=False, session_factory=factory) == 1
    assert (await _master(factory, known)).is_stock_managed is False


async def test_master_without_a_snapshot_is_still_flagged(_test_engine, tmp_path) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    mid = await _seed(factory, "H6", None)

    await run(
        csv_path=_csv(tmp_path, "sku_code,kind\nH6,made_to_order\n"),
        dry_run=False,
        session_factory=factory,
    )

    assert (await _master(factory, mid)).is_stock_managed is False
    assert await _qty(factory, mid) is None


async def test_header_aliases_from_the_clients_sheet_are_accepted(_test_engine, tmp_path) -> None:
    """The client fills the template in Excel; the columns come back Japanese."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    mid = await _seed(factory, "H7", -3)

    await run(
        csv_path=_csv(tmp_path, "SKUコード,種別\nH7,packaging\n", encoding="cp932"),
        dry_run=False,
        session_factory=factory,
    )

    assert (await _master(factory, mid)).is_stock_managed is False
    assert await _qty(factory, mid) == 0

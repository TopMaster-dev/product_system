"""The health check's queries against a real database.

Two properties the unit tests cannot reach. First, that the lock-free drift
query names exactly the SKUs `recompute(dry_run=True)` does — it replaces that
call in the check, so a disagreement would be a silent change of verdict.
Second, that the READ ONLY transaction genuinely refuses a write: that is the
whole basis for running this against production on a review morning.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli.recompute_snapshots import drift_stmt, recompute
from app.cli.verify_production_health import (
    Outcome,
    check_migration,
    check_snapshots,
    collect,
    run_check,
    script_head,
)
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku

pytestmark = pytest.mark.integration

WHEN = datetime(2026, 6, 1, tzinfo=UTC)


async def _master(session: AsyncSession, code: str) -> MasterSku:
    master = MasterSku(sku_code=code, name=code)
    session.add(master)
    await session.flush()
    return master


async def _events(session: AsyncSession, master: MasterSku, *deltas: int) -> int | None:
    last: int | None = None
    for delta in deltas:
        event = InventoryEvent(
            master_sku_id=master.id,
            event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
            quantity_delta=delta,
            occurred_at=WHEN,
        )
        session.add(event)
        await session.flush()
        last = event.id
    return last


async def _snapshot(session: AsyncSession, master: MasterSku, qty: int, last: int | None) -> None:
    session.add(InventorySnapshot(master_sku_id=master.id, on_hand_qty=qty, last_event_id=last))
    await session.flush()


async def _seed_every_shape(session: AsyncSession) -> dict[str, int]:
    """One SKU per way a snapshot can agree or disagree with its events."""
    ids: dict[str, int] = {}

    ok = await _master(session, "OK")
    await _snapshot(session, ok, 7, await _events(session, ok, 10, -3))
    ids["ok"] = ok.id

    wrong_qty = await _master(session, "WRONG-QTY")
    await _snapshot(session, wrong_qty, 99, await _events(session, wrong_qty, 5))
    ids["wrong_qty"] = wrong_qty.id

    wrong_last = await _master(session, "WRONG-LAST")
    last = await _events(session, wrong_last, 4, 1)
    await _snapshot(session, wrong_last, 5, (last or 0) - 1)
    ids["wrong_last"] = wrong_last.id

    no_snapshot = await _master(session, "NO-SNAPSHOT")
    await _events(session, no_snapshot, 2)
    ids["no_snapshot"] = no_snapshot.id

    empty_ok = await _master(session, "EMPTY-OK")
    await _snapshot(session, empty_ok, 0, None)
    ids["empty_ok"] = empty_ok.id

    empty_wrong = await _master(session, "EMPTY-WRONG")
    await _snapshot(session, empty_wrong, 5, None)
    ids["empty_wrong"] = empty_wrong.id

    return ids


async def test_the_drift_query_names_the_same_skus_as_recompute(db_session) -> None:
    ids = await _seed_every_shape(db_session)

    from_query = {row[0] for row in (await db_session.execute(drift_stmt())).all()}
    results = await recompute(db_session, sorted(ids.values()), dry_run=True)
    from_recompute = {r["master_sku_id"] for r in results if r["drift"]}

    assert from_query == from_recompute
    assert from_query == {
        ids["wrong_qty"],
        ids["wrong_last"],
        ids["no_snapshot"],
        ids["empty_wrong"],
    }


async def test_consistent_snapshots_pass(db_session) -> None:
    ok = await _master(db_session, "OK")
    await _snapshot(db_session, ok, 7, await _events(db_session, ok, 10, -3))

    outcome = await check_snapshots(db_session)
    assert outcome.ok is True
    assert "乖離0件" in outcome.summary


async def test_drift_fails_the_check(db_session) -> None:
    await _seed_every_shape(db_session)

    outcome = await check_snapshots(db_session)
    assert outcome.ok is False
    assert "乖離 4件" in outcome.summary


async def test_a_write_inside_a_check_is_refused(_test_engine: AsyncEngine) -> None:
    """Not a promise about the checks' own SQL — a guarantee from Postgres."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)

    async def writes(session: AsyncSession) -> Outcome:
        await session.execute(
            text("INSERT INTO master_skus (sku_code, name, attributes) VALUES ('X', 'X', '{}')")
        )
        return Outcome(True, "wrote")

    result = await run_check(factory, "write", "cli", writes)

    assert result.ok is False
    assert "read-only transaction" in result.summary
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM master_skus")) == 0


async def test_the_migration_check_reads_the_recorded_version(_test_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    async with _test_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
    try:
        async with _test_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": script_head()}
            )
        assert (await run_check(factory, "m", "cli", check_migration)).ok is True

        async with _test_engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = '0001'"))
        behind = await run_check(factory, "m", "cli", check_migration)
        assert behind.ok is False
        assert "現在 0001" in behind.summary
    finally:
        async with _test_engine.begin() as conn:
            await conn.execute(text("DROP TABLE alembic_version"))


async def test_every_check_runs_against_a_real_schema(_test_engine: AsyncEngine) -> None:
    """Each check's SQL executes inside a READ ONLY transaction without error.

    An empty database fails several checks on purpose (no rollup has run, no
    migration table) — what this pins is that none of them CRASHES, i.e. none
    issues a statement Postgres refuses in a read-only transaction, such as the
    `FOR UPDATE` that `recompute` takes."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    checks = await collect(days=7, session_factory=factory)

    assert [c.name for c in checks] == [
        "マイグレーション",
        "売上数値の突合",
        "定期ジョブ",
        "索引",
        "在庫スナップショット",
        "在庫イベント",
    ]
    crashed = [
        (c.name, c.summary)
        for c in checks
        if c.summary.startswith("確認できませんでした") and c.name != "マイグレーション"
    ]
    assert crashed == []
    by_name = {c.name: c for c in checks}
    assert by_name["在庫スナップショット"].ok is True
    assert by_name["在庫イベント"].ok is True
    assert by_name["定期ジョブ"].ok is False

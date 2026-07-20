"""Unit tests for reconcile_inventory CLI (Phase 1-B F1.8, variant-level).

The CSV aggregation helper is pure-Python (no DB), tested in isolation with
on-disk fixture CSVs. The full-flow / collect_diffs tests that touch the
channel='crossmall' mappings are marked integration and run against Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli.reconcile_inventory import (
    aggregate_csv_variants,
    collect_diffs,
    resolve_csv_arg,
    run,
)
from app.models import (
    ChannelSkuMapping,
    InventorySnapshot,
    MasterSku,
    ReconcileDiff,
    ReconcileRun,
)
from app.services.reconcile import DiffInput


def _factory_for_engine(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


ENC = "cp932"


def _write_csv(tmp_path: Path, name: str, rows: list[list[str]]) -> Path:
    import csv as _csv

    p = tmp_path / name
    with p.open("w", encoding=ENC, newline="") as f:
        w = _csv.writer(f)
        for row in rows:
            w.writerow(row)
    return p


# ---------- aggregate_csv_variants (pure unit) ----------


@pytest.mark.unit
def test_aggregate_keys_each_variant(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "ABC", "gold", "S", "10"],
            ["u", "ABC", "gold", "M", "5"],
            ["u", "ABC", "silver", "S", "3"],
            ["u", "XYZ", "", "", "7"],
        ],
    )
    agg = aggregate_csv_variants(csv_path)
    assert agg == {"ABC|gold|S": 10, "ABC|gold|M": 5, "ABC|silver|S": 3, "XYZ||": 7}


@pytest.mark.unit
def test_aggregate_sums_repeated_variant_and_skips_empty(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "A", "gold", "", "5"],
            ["u", "A", "gold", "", "4"],  # same variant -> summed
            ["u", "", "x", "y", "99"],  # empty code
            ["u", "A", "gold", "", ""],  # empty qty
            ["u", "B", "", "", "0"],
        ],
    )
    agg = aggregate_csv_variants(csv_path)
    assert agg == {"A|gold|": 9, "B||": 0}


@pytest.mark.unit
def test_aggregate_handles_negative_and_unparseable(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "H1", "", "", "-100"],
            ["u", "H1", "", "", "-25"],  # -> H1|| = -125
            ["u", "K", "gold", "", "10"],
            ["u", "K", "gold", "", "not-a-number"],  # skipped
        ],
    )
    agg = aggregate_csv_variants(csv_path)
    assert agg == {"H1||": -125, "K|gold|": 10}


# ---------- collect_diffs + full run (integration) ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_diffs_matches_via_crossmall_and_sums_aliases(
    db_session: AsyncSession,
) -> None:
    m = MasterSku(
        sku_code="N23gold",
        name="N23 gold",
        attributes={"token": "N23", "color": "gold", "size": ""},
    )
    db_session.add(m)
    await db_session.flush()
    db_session.add(InventorySnapshot(master_sku_id=m.id, on_hand_qty=10))
    db_session.add_all(
        [
            ChannelSkuMapping(
                master_sku_id=m.id, channel="crossmall", channel_sku="006c|gold|", is_active=True
            ),
            ChannelSkuMapping(
                master_sku_id=m.id, channel="crossmall", channel_sku="N23|gold|", is_active=True
            ),
        ]
    )
    await db_session.flush()

    diffs, summary = await collect_diffs(
        {"006c|gold|": 0, "N23|gold|": 27, "ORPHAN|gold|": 99},
        db_session,
    )
    assert len(diffs) == 1  # aliases 0 + 27 -> 27 vs snapshot 10
    assert diffs[0].master_sku_id == m.id
    assert diffs[0].current_qty == 10
    assert diffs[0].target_qty == 27
    assert summary["unmapped_keys"] == 1  # ORPHAN not mapped
    assert summary["matched_masters"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_diffs_excludes_bundle_masters(db_session: AsyncSession) -> None:
    parent = MasterSku(sku_code="N21gold", name="set", attributes={}, is_bundle=True)
    db_session.add(parent)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=parent.id, channel="crossmall", channel_sku="0010c|gold|", is_active=True
        )
    )
    await db_session.flush()
    diffs, summary = await collect_diffs({"0010c|gold|": 5}, db_session)
    assert diffs == []
    assert summary["excluded_bundles"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_diffs_master_without_snapshot(db_session: AsyncSession) -> None:
    m = MasterSku(sku_code="NEW-1", name="N", attributes={})
    db_session.add(m)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=m.id, channel="crossmall", channel_sku="k|gold|", is_active=True
        )
    )
    await db_session.flush()
    diffs, _summary = await collect_diffs({"k|gold|": 50}, db_session)
    assert len(diffs) == 1
    assert diffs[0].current_qty == 0
    assert diffs[0].target_qty == 50
    assert diffs[0].master_sku_id == m.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_run_persists_reconcile_run_with_diffs(
    _test_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        m = MasterSku(
            sku_code="RUNB",
            name="B",
            attributes={"token": "RB", "color": "gold", "size": ""},
        )
        setup.add(m)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=m.id, on_hand_qty=8))
        setup.add(
            ChannelSkuMapping(
                master_sku_id=m.id, channel="crossmall", channel_sku="RUNB|gold|", is_active=True
            )
        )
        m_id = m.id

    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "RUNB", "gold", "", "10"],  # -> RUNB|gold| , +2 delta
        ],
    )
    exit_code = await run(csv_path, triggered_by="test-runner", session_factory=factory)
    assert exit_code == 0

    async with factory() as verify:
        runs = (await verify.execute(select(ReconcileRun))).scalars().all()
        assert len(runs) == 1
        diffs = (
            (
                await verify.execute(
                    select(ReconcileDiff).where(ReconcileDiff.reconcile_run_id == runs[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert len(diffs) == 1
        assert diffs[0].master_sku_id == m_id
        assert diffs[0].delta == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_does_not_persist(_test_engine: AsyncEngine, tmp_path: Path) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        m = MasterSku(sku_code="DRY-A", name="A", attributes={})
        setup.add(m)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=m.id, on_hand_qty=2))
        setup.add(
            ChannelSkuMapping(
                master_sku_id=m.id, channel="crossmall", channel_sku="DRY-A||", is_active=True
            )
        )

    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "DRY-A", "", "", "10"],
        ],
    )
    exit_code = await run(csv_path, triggered_by="dry", dry_run=True, session_factory=factory)
    assert exit_code == 0
    async with factory() as verify:
        runs = (await verify.execute(select(ReconcileRun))).scalars().all()
        assert runs == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_csv_returns_exit_2(_test_engine: AsyncEngine, tmp_path: Path) -> None:
    factory = _factory_for_engine(_test_engine)
    exit_code = await run(tmp_path / "does-not-exist.csv", session_factory=factory)
    assert exit_code == 2


# ---------- DiffInput plumbing sanity ----------


@pytest.mark.unit
def test_diff_input_dataclass_is_frozen() -> None:
    d = DiffInput(master_sku_id=1, current_qty=2, target_qty=3)
    with pytest.raises(AttributeError):
        d.master_sku_id = 99  # type: ignore[misc]


# ---------- resolve_csv_arg (gs:// + local) ----------


@pytest.mark.unit
def test_resolve_csv_arg_local_path_passes_through(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("dummy", encoding="utf-8")
    assert resolve_csv_arg(p) == p


@pytest.mark.unit
def test_resolve_csv_arg_local_path_string_becomes_path(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("dummy", encoding="utf-8")
    assert resolve_csv_arg(str(p)) == p


@pytest.mark.unit
def test_resolve_csv_arg_gs_uri_delegates_to_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloaded = tmp_path / "downloaded.csv"
    downloaded.write_text("payload", encoding="utf-8")
    captured: list[str] = []

    def fake_download(uri: str) -> Path:
        captured.append(uri)
        return downloaded

    monkeypatch.setattr("app.cli.reconcile_inventory._download_gs", fake_download)
    out = resolve_csv_arg("gs://product-system-verify/recon/x.csv")
    assert out == downloaded
    assert captured == ["gs://product-system-verify/recon/x.csv"]


@pytest.mark.unit
def test_run_resolves_gs_uri_before_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run() forwards a gs:// arg through resolve_csv_arg; the downloaded Path is
    what aggregate_csv_variants reads. A stub session returns no mappings, so the
    dry-run produces zero diffs and exits 0 without a DB."""
    import asyncio

    local_csv = _write_csv(
        tmp_path,
        "fake.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "GS-A", "gold", "", "3"],
        ],
    )

    def fake_download(uri: str) -> Path:
        assert uri == "gs://b/x.csv"
        return local_csv

    monkeypatch.setattr("app.cli.reconcile_inventory._download_gs", fake_download)

    class _FakeSession:
        async def execute(self, *_a, **_kw):  # type: ignore[no-untyped-def]
            class _R:
                @staticmethod
                def all():
                    return []

                @staticmethod
                def scalars():
                    return _R()

            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def begin(self):
            return self

    def _factory():
        return _FakeSession()

    exit_code = asyncio.run(
        run("gs://b/x.csv", triggered_by="t", dry_run=True, session_factory=_factory)
    )
    assert exit_code == 0


@pytest.mark.unit
def test_download_gs_rejects_malformed_uri() -> None:
    from app.cli import reconcile_inventory as ri

    with pytest.raises(ValueError, match="malformed gs:// URI"):
        ri._download_gs("gs://only-bucket")

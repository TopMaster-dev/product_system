"""Unit tests for reconcile_inventory CLI (Phase 1-B F1.8).

The CSV aggregation helper is pure-Python (no DB), so tested in isolation
with on-disk fixture CSVs. The full-flow test that actually creates a
ReconcileRun is marked integration and runs against the test Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cli.reconcile_inventory import (
    aggregate_csv_by_product,
    collect_diffs,
    resolve_csv_arg,
    run,
)
from app.models import InventorySnapshot, MasterSku, ReconcileDiff, ReconcileRun
from app.services.reconcile import DiffInput


def _factory_for_engine(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the test engine, with the same defaults the
    fixture-managed session uses so the CLI sees fixture-seeded data."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


# CP932 (Shift-JIS) encoding for the test fixtures so we exercise the real
# decoder path.
ENC = "cp932"


def _write_csv(tmp_path: Path, name: str, rows: list[list[str]]) -> Path:
    """Write a CP932-encoded CSV to a tmp file. Quoting follows csv module
    defaults so commas in headers/values stay intact."""
    import csv as _csv

    p = tmp_path / name
    with p.open("w", encoding=ENC, newline="") as f:
        w = _csv.writer(f)
        for row in rows:
            w.writerow(row)
    return p


# ---------- aggregate_csv_by_product (pure unit) ----------


@pytest.mark.unit
def test_aggregate_sums_variants_per_product(tmp_path: Path) -> None:
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
    agg = aggregate_csv_by_product(csv_path)
    assert agg == {"ABC": 18, "XYZ": 7}


@pytest.mark.unit
def test_aggregate_skips_empty_codes_and_qtys(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "A", "", "", "5"],
            ["u", "", "x", "y", "99"],
            ["u", "A", "", "", ""],
            ["u", "B", "", "", "0"],
        ],
    )
    agg = aggregate_csv_by_product(csv_path)
    assert agg == {"A": 5, "B": 0}


@pytest.mark.unit
def test_aggregate_handles_negative_qty(tmp_path: Path) -> None:
    """CROSS MALL itself sometimes carries negative on-hand values; we
    preserve them as-is so the reconcile reflects reality."""
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "H1", "", "", "-100"],
            ["u", "H1", "", "", "-25"],
        ],
    )
    agg = aggregate_csv_by_product(csv_path)
    assert agg == {"H1": -125}


@pytest.mark.unit
def test_aggregate_skips_unparseable_qty(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "K", "", "", "10"],
            ["u", "K", "", "", "not-a-number"],
            ["u", "K", "", "", "5"],
        ],
    )
    agg = aggregate_csv_by_product(csv_path)
    assert agg == {"K": 15}  # bad row skipped


# ---------- collect_diffs + full run (integration) ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_diffs_with_matched_skus(db_session: AsyncSession) -> None:
    # Seed: two masters with snapshots
    sku_a = MasterSku(sku_code="MATCH-A", name="A", attributes={})
    sku_b = MasterSku(sku_code="MATCH-B", name="B", attributes={})
    db_session.add_all([sku_a, sku_b])
    await db_session.flush()
    db_session.add_all(
        [
            InventorySnapshot(master_sku_id=sku_a.id, on_hand_qty=10),
            InventorySnapshot(master_sku_id=sku_b.id, on_hand_qty=20),
        ]
    )
    await db_session.flush()

    diffs, summary = await collect_diffs(
        {
            "MATCH-A": 12,  # +2 delta
            "MATCH-B": 20,  # no delta
            "ORPHAN-Z": 99,  # not in master_skus
        },
        db_session,
    )
    diffs_by_sku = {d.master_sku_id: d for d in diffs}
    assert sku_a.id in diffs_by_sku
    assert sku_b.id not in diffs_by_sku
    assert diffs_by_sku[sku_a.id].current_qty == 10
    assert diffs_by_sku[sku_a.id].target_qty == 12
    assert summary["csv_unique_codes"] == 3
    assert summary["csv_codes_not_in_master"] == 1
    assert summary["matched_sku_count"] == 2
    assert summary["actual_diffs"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_diffs_for_master_without_snapshot(db_session: AsyncSession) -> None:
    """A master_sku that has no snapshot row is treated as current=0."""
    sku = MasterSku(sku_code="NEW-1", name="N", attributes={})
    db_session.add(sku)
    await db_session.flush()
    diffs, _summary = await collect_diffs({"NEW-1": 50}, db_session)
    assert len(diffs) == 1
    assert diffs[0].current_qty == 0
    assert diffs[0].target_qty == 50
    assert diffs[0].master_sku_id == sku.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_run_persists_reconcile_run_with_diffs(
    _test_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    factory = _factory_for_engine(_test_engine)
    # Seed masters + snapshots in their own committed session so the CLI's
    # subsequent reads (using the same engine) see them.
    async with factory() as setup, setup.begin():
        sku_a = MasterSku(sku_code="RUN-A", name="A", attributes={})
        sku_b = MasterSku(sku_code="RUN-B", name="B", attributes={})
        setup.add_all([sku_a, sku_b])
        await setup.flush()
        setup.add_all(
            [
                InventorySnapshot(master_sku_id=sku_a.id, on_hand_qty=5),
                InventorySnapshot(master_sku_id=sku_b.id, on_hand_qty=8),
            ]
        )
        sku_a_id = sku_a.id
        sku_b_id = sku_b.id

    csv_path = _write_csv(
        tmp_path,
        "inv.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "RUN-A", "x", "", "5"],  # no delta
            ["u", "RUN-A", "y", "", "0"],
            ["u", "RUN-B", "x", "", "10"],  # +2 delta
            ["u", "RUN-B", "y", "", "0"],
        ],
    )
    exit_code = await run(csv_path, triggered_by="test-runner", session_factory=factory)
    assert exit_code == 0

    async with factory() as verify:
        runs = (await verify.execute(select(ReconcileRun))).scalars().all()
        assert len(runs) == 1
        run_row = runs[0]
        assert run_row.source == "cross_mall_csv"
        assert run_row.triggered_by == "test-runner"
        # Only RUN-B has a delta -> one diff row
        diffs = (
            (
                await verify.execute(
                    select(ReconcileDiff).where(ReconcileDiff.reconcile_run_id == run_row.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(diffs) == 1
        assert diffs[0].master_sku_id == sku_b_id
        assert diffs[0].delta == 2
        assert sku_a_id  # silence unused-var on the seeded id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_does_not_persist(_test_engine: AsyncEngine, tmp_path: Path) -> None:
    factory = _factory_for_engine(_test_engine)
    async with factory() as setup, setup.begin():
        sku = MasterSku(sku_code="DRY-A", name="A", attributes={})
        setup.add(sku)
        await setup.flush()
        setup.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=2))

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
    out = resolve_csv_arg(p)
    assert out == p


@pytest.mark.unit
def test_resolve_csv_arg_local_path_string_becomes_path(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("dummy", encoding="utf-8")
    out = resolve_csv_arg(str(p))
    assert out == p


@pytest.mark.unit
def test_resolve_csv_arg_gs_uri_delegates_to_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gs:// branches go through _download_gs so tests can stub it without
    pulling google-cloud-storage into the test runtime."""
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
    """run() forwards a gs:// arg through resolve_csv_arg; the downloaded
    Path is what aggregate_csv_by_product sees. We stub _download_gs to a
    canned CSV and force exit-2 by leaving the master_sku table empty —
    but session_factory is patched to never be called because we monkeypatch
    aggregate_csv_by_product to a no-op and supply a stub session_factory."""
    import asyncio

    local_csv = _write_csv(
        tmp_path,
        "fake.csv",
        [
            ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"],
            ["u", "GS-A", "", "", "3"],
        ],
    )

    def fake_download(uri: str) -> Path:
        assert uri == "gs://b/x.csv"
        return local_csv

    monkeypatch.setattr("app.cli.reconcile_inventory._download_gs", fake_download)

    # Stub the session factory so the test stays a unit test (no DB).
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
    """A gs:// URI without an object key must raise ValueError before any
    network call. The malformed-URI check runs before the storage import,
    so this test is safe even without google-cloud-storage installed."""
    from app.cli import reconcile_inventory as ri

    with pytest.raises(ValueError, match="malformed gs:// URI"):
        ri._download_gs("gs://only-bucket")

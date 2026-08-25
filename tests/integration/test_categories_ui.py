"""Integration tests — category master and the SKU-assignment CSV import.

Kept out of test_admin_ui.py, which is already long, but it reuses the same
admin_client fixture pattern: Basic Auth over ASGITransport with get_session
pointed at the test engine.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import InventorySnapshot, MasterSku, ProductCategory
from tests.integration.test_admin_ui import PASSWORD, USER, _auth_header

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin_client(_test_engine) -> AsyncIterator[AsyncClient]:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def _override_session():
        async with factory() as session:
            yield session

    test_settings = Settings(app_env="local", admin_username=USER, admin_password=PASSWORD)
    app.dependency_overrides[get_session] = _override_session
    from app.ui.auth import get_settings as auth_get_settings

    app.dependency_overrides[auth_get_settings] = lambda: test_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _seed_category(
    factory, code: str, name: str, *, parent_id: int | None = None, active: bool = True
) -> int:
    async with factory() as session, session.begin():
        category = ProductCategory(
            code=code,
            name=name,
            parent_id=parent_id,
            level=1 if parent_id is None else 2,
            is_active=active,
        )
        session.add(category)
        await session.flush()
        return category.id


async def _seed_sku(
    factory,
    code: str,
    *,
    category_id: int | None = None,
    archived: bool = False,
    unmanaged: bool = False,
) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(
            sku_code=code,
            name=code,
            category_id=category_id,
            is_stock_managed=not unmanaged,
            non_inventory_kind="packaging" if unmanaged else None,
            archived_at=datetime.now(UTC) if archived else None,
        )
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=1))
        return sku.id


async def _category_of(factory, sku_id: int) -> int | None:
    async with factory() as session:
        return await session.scalar(select(MasterSku.category_id).where(MasterSku.id == sku_id))


def _csv_bytes(text: str, encoding: str = "utf-8-sig") -> bytes:
    return text.encode(encoding)


# --- taxonomy ------------------------------------------------------------


async def test_screen_nests_children_under_their_parent(admin_client, _test_engine) -> None:
    # Codes deliberately unlike the form's placeholder examples — matching one
    # would find the placeholder instead of the rendered row.
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    root = await _seed_category(factory, "ZZTOP", "テスト大分類")
    await _seed_category(factory, "ZZTOP-SUB", "テスト中分類", parent_id=root)

    r = await admin_client.get("/admin/categories", headers=_auth_header())
    assert r.status_code == 200
    assert "テスト大分類" in r.text and "テスト中分類" in r.text
    # Parent row first, child after it carrying the indent marker.
    assert r.text.index("ZZTOP</td>") < r.text.index("ZZTOP-SUB</td>")
    child_row = r.text[r.text.index("ZZTOP-SUB</td>") :][:600]
    assert "└" in child_row


async def test_sku_counts_roll_up_to_the_parent(admin_client, _test_engine) -> None:
    """A 大分類 whose SKUs all live in its children must not read 0 — that looks
    like the import failed when it actually worked."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    root = await _seed_category(factory, "BR", "ブレスレット")
    child = await _seed_category(factory, "BR-CHAIN", "チェーン", parent_id=root)
    await _seed_sku(factory, "ROLL-1", category_id=child)
    await _seed_sku(factory, "ROLL-2", category_id=child)

    async with factory() as session:
        from app.services.categories import load_overview

        overview = await load_overview(session)

    by_code = {n.category.code: n for n in overview.roots}
    assert by_code["BR"].own_sku_count == 0
    assert by_code["BR"].total_sku_count == 2  # rolled up from the child


async def test_deactivating_a_parent_with_active_children_is_refused(
    admin_client, _test_engine
) -> None:
    """Otherwise the children stay assignable while invisible in every grouped
    view, so SKUs get filed into a branch nobody can see."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    root = await _seed_category(factory, "RG", "リング")
    await _seed_category(factory, "RG-PLAIN", "プレーン", parent_id=root)

    r = await admin_client.post(f"/admin/categories/{root}/toggle", headers=_auth_header())
    assert "flash=has_children" in r.headers["location"]

    async with factory() as session:
        parent = (
            await session.execute(select(ProductCategory).where(ProductCategory.id == root))
        ).scalar_one()
    assert parent.is_active is True


async def test_category_with_assigned_skus_cannot_be_deleted(admin_client, _test_engine) -> None:
    """ON DELETE RESTRICT protects the data; this turns it into a sentence an
    operator can act on."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "AN", "アンクレット")
    await _seed_sku(factory, "DEL-1", category_id=cat)

    r = await admin_client.post(f"/admin/categories/{cat}/delete", headers=_auth_header())
    assert "flash=in_use" in r.headers["location"]

    async with factory() as session:
        still_there = await session.scalar(
            select(func.count()).select_from(ProductCategory).where(ProductCategory.id == cat)
        )
    assert still_there == 1


async def test_two_top_level_categories_cannot_share_a_name(admin_client, _test_engine) -> None:
    """UNIQUE ... NULLS NOT DISTINCT. Postgres counts NULL parent_ids as distinct
    from each other, so the plain constraint would allow this."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_category(factory, "P1", "ピアス")

    r = await admin_client.post(
        "/admin/categories",
        data={"code": "P2", "name": "ピアス", "parent_id": ""},
        headers=_auth_header(),
    )
    assert "flash=duplicate" in r.headers["location"]


async def test_creating_with_a_parent_files_it_as_level_2(admin_client, _test_engine) -> None:
    """The level is derived, never asked for, so a 中分類 cannot be filed at the
    wrong depth and trip the CHECK."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    root = await _seed_category(factory, "WT", "ウォッチ")

    await admin_client.post(
        "/admin/categories",
        data={"code": "WT-LEATHER", "name": "レザー", "parent_id": str(root)},
        headers=_auth_header(),
    )
    async with factory() as session:
        child = (
            await session.execute(
                select(ProductCategory).where(ProductCategory.code == "WT-LEATHER")
            )
        ).scalar_one()
    assert child.level == 2
    assert child.parent_id == root


# --- CSV import ----------------------------------------------------------


async def test_preview_writes_nothing(admin_client, _test_engine) -> None:
    """The 確認 step must be provably read-only — that is the entire reason it
    sits between upload and execute."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_category(factory, "C1", "カテゴリ1")
    sku = await _seed_sku(factory, "PRE-1")

    body = _csv_bytes("# 説明行です\nsku_code,category_code\nPRE-1,C1\n")
    r = await admin_client.post(
        "/admin/categories/upload",
        files={"file": ("sku_categories.csv", body, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert await _category_of(factory, sku) is None


async def test_apply_assigns_and_is_idempotent(admin_client, _test_engine) -> None:
    """Re-sending a corrected file has to be safe; the second pass is a no-op."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "C2", "カテゴリ2")
    sku = await _seed_sku(factory, "APP-1")

    payload = {
        "csv_b64": base64.b64encode(_csv_bytes("sku_code,category_code\nAPP-1,C2\n")).decode()
    }
    for _ in range(2):
        r = await admin_client.post(
            "/admin/categories/upload/apply", data=payload, headers=_auth_header()
        )
        assert "flash=imported" in r.headers["location"]
        assert await _category_of(factory, sku) == cat


async def test_blank_category_code_clears_the_assignment(admin_client, _test_engine) -> None:
    """An empty cell un-assigns. Without it there is no way to undo a mis-import
    from the same screen."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "C3", "カテゴリ3")
    sku = await _seed_sku(factory, "CLR-1", category_id=cat)

    await admin_client.post(
        "/admin/categories/upload/apply",
        data={"csv_b64": base64.b64encode(_csv_bytes("sku_code,category_code\nCLR-1,\n")).decode()},
        headers=_auth_header(),
    )
    assert await _category_of(factory, sku) is None


async def test_unknown_codes_are_reported_and_skipped(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "UNK-1")

    body = _csv_bytes("sku_code,category_code\nUNK-1,NO-SUCH-CAT\nNO-SUCH-SKU,C1\n")
    r = await admin_client.post(
        "/admin/categories/upload",
        files={"file": ("f.csv", body, "text/csv")},
        headers=_auth_header(),
    )
    assert "NO-SUCH-CAT" in r.text
    assert "NO-SUCH-SKU" in r.text


async def test_japanese_headers_and_cp932_are_accepted(admin_client, _test_engine) -> None:
    """The client edits the template in Excel; it comes back CP932 with Japanese
    column names."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "C4", "カテゴリ4")
    sku = await _seed_sku(factory, "JP-1")

    body = _csv_bytes("SKUコード,カテゴリコード\nJP-1,C4\n", encoding="cp932")
    await admin_client.post(
        "/admin/categories/upload/apply",
        data={"csv_b64": base64.b64encode(body).decode()},
        headers=_auth_header(),
    )
    assert await _category_of(factory, sku) == cat


async def test_apply_rejects_a_corrupt_hidden_field(admin_client, _test_engine) -> None:
    """csv_b64 is client-controlled input, so it is decoded defensively rather
    than trusted because the preview produced it."""
    r = await admin_client.post(
        "/admin/categories/upload/apply",
        data={"csv_b64": "not-valid-base64!!!"},
        headers=_auth_header(),
    )
    assert "flash=invalid" in r.headers["location"]


async def test_apply_revalidates_instead_of_trusting_the_preview(
    admin_client, _test_engine
) -> None:
    """A category deleted between preview and execute must not be applied — the
    preview proves what was true a moment ago, not what is true now."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "C5", "カテゴリ5")
    sku = await _seed_sku(factory, "REV-1")

    body = _csv_bytes("sku_code,category_code\nREV-1,C5\n")
    payload = {"csv_b64": base64.b64encode(body).decode()}

    async with factory() as session, session.begin():
        await session.execute(
            update(ProductCategory).where(ProductCategory.id == cat).values(code="C5-RENAMED")
        )

    await admin_client.post("/admin/categories/upload/apply", data=payload, headers=_auth_header())
    # The code in the file no longer resolves, so nothing is assigned.
    assert await _category_of(factory, sku) is None


# --- counting population -------------------------------------------------


async def test_unclassified_count_excludes_archived_and_unmanaged(
    admin_client, _test_engine
) -> None:
    """未分類件数 must use the same population as the inventory list. Counting
    the ~355 masters the archive cleanup retired would send the client hunting
    for work that does not exist."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    from app.services.categories import count_unclassified

    async with factory() as session:
        before = await count_unclassified(session)

    await _seed_sku(factory, "POP-LIVE")
    await _seed_sku(factory, "POP-ARCHIVED", archived=True)
    await _seed_sku(factory, "POP-BOX", unmanaged=True)

    async with factory() as session:
        after = await count_unclassified(session)

    assert after - before == 1, "only the live SKU should count as 未分類"


async def test_categories_export_csv_round_trips_the_taxonomy(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    root = await _seed_category(factory, "EX", "エクスポート")
    await _seed_category(factory, "EX-SUB", "サブ", parent_id=root)

    r = await admin_client.get("/admin/categories/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "category_code,大分類,中分類,有効,割当SKU数" in r.text
    assert "EX,エクスポート,," in r.text
    assert "EX-SUB,エクスポート,サブ," in r.text


async def test_import_categories_cli_shares_the_screen_s_logic(_test_engine, tmp_path) -> None:
    """The CLI and the upload screen must never disagree about what a file
    means, which is why both call plan_assignments."""
    from app.cli.import_categories import run

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    cat = await _seed_category(factory, "CLI-1", "CLI用カテゴリ")
    sku = await _seed_sku(factory, "CLI-SKU-1")

    path = tmp_path / "sku_categories.csv"
    path.write_bytes("sku_code,category_code\nCLI-SKU-1,CLI-1\n".encode("utf-8-sig"))

    assert await run(csv_path=path, dry_run=True, session_factory=factory) == 0
    assert await _category_of(factory, sku) is None, "dry-run must not write"

    assert await run(csv_path=path, dry_run=False, session_factory=factory) == 0
    assert await _category_of(factory, sku) == cat


async def test_import_categories_cli_reports_unknown_codes_by_name(_test_engine, tmp_path) -> None:
    """ "12 unknown categories" is not something anyone can act on."""
    from app.cli import import_categories as mod

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "CLI-SKU-2")

    path = tmp_path / "bad.csv"
    path.write_bytes("sku_code,category_code\nCLI-SKU-2,NOPE\n".encode("utf-8-sig"))

    captured: dict[str, object] = {}
    original = mod.log.info
    mod.log.info = lambda event, **kw: captured.update(kw)
    try:
        code = await mod.run(csv_path=path, dry_run=True, session_factory=factory)
    finally:
        mod.log.info = original

    assert code == 1, "a skipped row must give a non-zero exit for scripted runs"
    assert captured["unknown_category_examples"] == ["NOPE"]

"""The four follow-ups from the client's acceptance Q&A (2026-10-12).

1. The category import preview names each product and its before / after.
2. Every data-quality tile opens the list of exactly the products it counts.
3. (9/29 difference — explained, no code.)
4. A set's own page can be reached again: search, the component's panel, and
   links between a set and its components in both directions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import BundleComponent, ChannelSkuMapping, InventorySnapshot, MasterSku
from app.services.categories import plan_assignments
from app.services.data_quality import SKU_LIST_KINDS, sku_list, sku_list_href, summary_tiles
from tests.integration.test_admin_ui import PASSWORD, USER, _auth_header
from tests.integration.test_categories_ui import _csv_bytes, _seed_category, _seed_sku

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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# --- 1. カテゴリCSVの確認画面 ----------------------------------------------


async def test_the_preview_names_each_product_and_its_before_and_after(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ring = await _seed_category(factory, "RING", "リング")
    signet = await _seed_category(factory, "RING-SIG", "印台", parent_id=ring)
    await _seed_sku(factory, "MOVE-1")
    await _seed_sku(factory, "STAY-1", category_id=signet)
    await _seed_sku(factory, "CLEAR-1", category_id=ring)

    body = _csv_bytes("sku_code,category_code\nMOVE-1,RING-SIG\nSTAY-1,RING-SIG\nCLEAR-1,\n")
    r = await admin_client.post(
        "/admin/categories/upload",
        files={"file": ("c.csv", body, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "変更される商品" in r.text and "2件" in r.text
    table = r.text.split("変更される商品", 1)[1]
    assert "MOVE-1" in table and "リング / 印台" in table  # 中分類 shown with its 大分類
    assert "CLEAR-1" in table and "未分類 (解除)" in table
    assert "STAY-1" not in table  # unchanged rows are not listed


async def test_a_sku_listed_twice_takes_its_last_row(db_session) -> None:
    """The plan used to queue every row under its target, so which of two rows
    for the same SKU won depended on category order, not on the file."""
    a = await _seed(db_session, category="A")
    b = await _seed(db_session, category="B")
    db_session.add(MasterSku(sku_code="DUP-1", name="DUP-1"))
    await db_session.flush()

    body = b"sku_code,category_code\nDUP-1,B\nDUP-1,A\n"
    _, plan = await plan_assignments(db_session, body)

    assert plan.duplicate_skus == ["DUP-1"]
    assert {k: len(v) for k, v in plan.by_category.items()} == {a: 1}
    assert [c.after for c in plan.changes] == ["A"]
    assert b not in plan.by_category


async def _seed(session, *, category: str) -> int:
    from app.models import ProductCategory

    c = ProductCategory(code=category, name=category, level=1)
    session.add(c)
    await session.flush()
    return c.id


async def test_inventory_shows_each_skus_category(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    ring = await _seed_category(factory, "RING", "リング")
    signet = await _seed_category(factory, "RING-SIG", "印台", parent_id=ring)
    sku_id = await _seed_sku(factory, "CAT-SHOWN", category_id=signet)

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    panel = r.text.split(f'<template id="inv-{sku_id}"', 1)[1].split("</template>", 1)[0]
    assert "リング / 印台" in panel

    csv_text = (await admin_client.get("/admin/inventory/export.csv", headers=_auth_header())).text
    header, *lines = csv_text.lstrip("﻿").splitlines()
    assert header.endswith(",category")  # appended, never inserted
    assert any(line.startswith("CAT-SHOWN,") and line.endswith("リング / 印台") for line in lines)


# --- 2. データ品質のカード ---------------------------------------------------


async def test_every_list_tile_counts_exactly_the_rows_its_list_shows(db_session) -> None:
    pool = MasterSku(sku_code="POOL", name="POOL", image_url="https://x/img.png")
    parent = MasterSku(sku_code="SET-1", name="SET-1", is_bundle=True)
    bare = MasterSku(sku_code="BARE", name="BARE")
    gift = MasterSku(
        sku_code="GIFT", name="GIFT", is_stock_managed=False, non_inventory_kind="gift"
    )
    db_session.add_all([pool, parent, bare, gift])
    await db_session.flush()
    db_session.add(
        BundleComponent(
            bundle_master_sku_id=parent.id, component_master_sku_id=pool.id, quantity_per=1
        )
    )
    db_session.add(
        ChannelSkuMapping(
            channel="shopify", channel_sku="POOL", master_sku_id=pool.id, is_active=True
        )
    )
    await db_session.flush()

    tiles = {t.key: t for t in await summary_tiles(db_session, placeholder_pattern="^x$")}
    for key in SKU_LIST_KINDS:
        total, rows = await sku_list(db_session, key)
        assert tiles[key].count == total == len(rows), key
        assert tiles[key].href == sku_list_href(key), key

    _, bundles = await sku_list(db_session, "bundles")
    assert [r["sku_code"] for r in bundles] == ["SET-1"]
    _, no_mapping = await sku_list(db_session, "no_mapping")
    assert "POOL" not in {r["sku_code"] for r in no_mapping}


async def test_a_tile_list_page_shows_its_products_and_the_way_to_fix_them(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "NO-CAT-1")

    r = await admin_client.get(sku_list_href("no_category"), headers=_auth_header())
    assert r.status_code == 200
    assert "NO-CAT-1" in r.text
    assert 'href="/admin/categories/upload"' in r.text

    csv_text = (
        await admin_client.get(
            "/admin/data-quality/skus/export.csv?kind=no_category", headers=_auth_header()
        )
    ).text
    # Fill in category_code and it goes straight back into the category import.
    assert csv_text.lstrip("﻿").startswith("sku_code,category_code,")
    assert "NO-CAT-1,," in csv_text

    missing = await admin_client.get("/admin/data-quality/skus?kind=nope", headers=_auth_header())
    assert missing.status_code == 404


# --- 4. セット商品への導線 ---------------------------------------------------


async def _seed_set(factory) -> tuple[int, int, int]:
    async with factory() as session, session.begin():
        pool = MasterSku(sku_code="N108silver", name="N108 silver")
        p45 = MasterSku(sku_code="N108silver45", name="Anchor Necklace 45cm", is_bundle=True)
        p42 = MasterSku(sku_code="N108silver42", name="Anchor Necklace 42cm", is_bundle=True)
        session.add_all([pool, p45, p42])
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=pool.id, on_hand_qty=21))
        for parent in (p45, p42):
            session.add(
                BundleComponent(
                    bundle_master_sku_id=parent.id,
                    component_master_sku_id=pool.id,
                    quantity_per=1,
                )
            )
        return pool.id, p45.id, p42.id


async def test_searching_inventory_for_a_set_points_to_its_pages(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    pool, p45, p42 = await _seed_set(factory)

    r = await admin_client.get("/admin/inventory?q=N108", headers=_auth_header())
    box = r.text.split("に該当するセット商品・共有在庫", 1)[1].split("</div>\n</div>", 1)[0]
    assert f'href="/admin/analytics/sku/{p45}"' in box
    assert f'href="/admin/analytics/sku/{p42}"' in box

    panel = r.text.split(f'<template id="inv-{pool}"', 1)[1].split("</template>", 1)[0]
    assert "この在庫を使うセット商品" in panel
    assert f"/admin/analytics/sku/{p45}" in panel


async def test_a_set_and_its_component_link_to_each_other(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    pool, p45, _ = await _seed_set(factory)

    parent_page = await admin_client.get(f"/admin/analytics/sku/{p45}", headers=_auth_header())
    assert "在庫はこちらで管理しています" in parent_page.text
    assert f"/admin/analytics/sku/{pool}?" in parent_page.text

    component_page = await admin_client.get(f"/admin/analytics/sku/{pool}", headers=_auth_header())
    assert "この在庫を使うセット商品" in component_page.text
    assert f"/admin/analytics/sku/{p45}?" in component_page.text


async def test_the_sales_screen_search_finds_sets(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    _, p45, p42 = await _seed_set(factory)

    r = await admin_client.get("/admin/analytics/sales?q=N108silver4", headers=_auth_header())
    assert r.status_code == 200
    results = r.text.split("の検索結果", 1)[1]
    assert f"/admin/analytics/sku/{p45}?" in results and f"/admin/analytics/sku/{p42}?" in results
    assert "セット" in results

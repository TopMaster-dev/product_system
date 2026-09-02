"""Integration tests — data-quality summary and the Rakuten SKU defect report.

The load-bearing test here is `test_every_tile_link_resolves`: a tile is a count
with somewhere to go, and a count whose link 404s is worse than no tile at all —
it sends an operator looking for a screen that does not exist.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import (
    ChannelSkuMapping,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
)
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


async def _seed_sku(
    factory,
    code: str,
    *,
    qty: int | None = None,
    image: str | None = None,
    archived: bool = False,
    unmanaged: bool = False,
    bundle: bool = False,
) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(
            sku_code=code,
            name=code,
            image_url=image,
            is_bundle=bundle,
            is_stock_managed=not unmanaged,
            non_inventory_kind="packaging" if unmanaged else None,
            archived_at=datetime.now(UTC) if archived else None,
        )
        session.add(sku)
        await session.flush()
        if qty is not None:
            session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=qty))
        return sku.id


async def _seed_rakuten_mapping(
    factory, master_id: int, channel_sku: str, *, marketplace_id: str | None = None
) -> None:
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=master_id,
                channel="rakuten",
                channel_sku=channel_sku,
                marketplace_id=marketplace_id,
                is_active=True,
            )
        )


async def _seed_unmapped_rakuten_line(
    factory, order_id: str, channel_sku: str, qty: int, price: str
) -> None:
    async with factory() as session, session.begin():
        order = Order(
            channel="rakuten",
            channel_order_id=order_id,
            status=OrderStatusEnum.CONFIRMED,
            ordered_at=datetime.now(UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                line_id="L-1",
                channel_sku=channel_sku,
                master_sku_id=None,
                quantity=qty,
                unit_price=Decimal(price),
            )
        )


# --- summary -------------------------------------------------------------


async def test_summary_renders_every_tile(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/data-quality", headers=_auth_header())
    assert r.status_code == 200
    for label in ("画像なし", "カテゴリ未設定", "マッピング欠落", "マイナス在庫", "仮値の楽天SKU"):
        assert label in r.text


async def test_every_tile_link_resolves(admin_client, _test_engine) -> None:
    """A count with a broken link is worse than no tile: it sends an operator
    looking for a screen that is not there. Follow every href the page renders."""
    r = await admin_client.get("/admin/data-quality", headers=_auth_header())
    hrefs = set(re.findall(r'href="(/admin/[^"]*)"', r.text))
    assert hrefs, "the summary rendered no links at all"

    for href in sorted(hrefs):
        target = await admin_client.get(href, headers=_auth_header())
        assert target.status_code == 200, f"{href} returned {target.status_code}"


async def test_deep_check_is_opt_in(admin_client, _test_engine) -> None:
    """The snapshot-vs-events aggregate is the most expensive query on the page
    and must not run on a casual visit to db-f1-micro."""
    # Match the tile's own hint, not the label — the footer explaining WHY the
    # check is opt-in names it too, on the page where it did not run.
    tile_marker = "0 が正常です。"

    normal = await admin_client.get("/admin/data-quality", headers=_auth_header())
    assert tile_marker not in normal.text

    deep = await admin_client.get("/admin/data-quality?deep=1", headers=_auth_header())
    assert tile_marker in deep.text
    assert "在庫イベント不整合" in deep.text


async def test_archived_skus_are_not_counted_as_defects(admin_client, _test_engine) -> None:
    """The archive cleanup deliberately retires ~355 masters. Counting them as
    "画像なし" would invent work that does not exist, and the tile would
    disagree with the screen it links to."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    from app.services.data_quality import summary_tiles

    async def counts() -> dict[str, int]:
        async with factory() as session:
            tiles = await summary_tiles(session, placeholder_pattern=r"^r-sku\d+$")
        return {t.key: t.count for t in tiles}

    before = await counts()
    await _seed_sku(factory, "DQ-ARCHIVED", qty=0, archived=True)
    await _seed_sku(factory, "DQ-BOX", qty=0, unmanaged=True)
    after = await counts()

    assert after["no_image"] == before["no_image"]
    assert after["no_category"] == before["no_category"]
    # They are reported, but as informational rather than as work.
    assert after["archived"] == before["archived"] + 1
    assert after["unmanaged"] == before["unmanaged"] + 1


async def test_live_defects_are_counted(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    from app.services.data_quality import summary_tiles

    async def counts() -> dict[str, int]:
        async with factory() as session:
            tiles = await summary_tiles(session, placeholder_pattern=r"^r-sku\d+$")
        return {t.key: t.count for t in tiles}

    before = await counts()
    await _seed_sku(factory, "DQ-NOIMAGE", qty=5)
    await _seed_sku(factory, "DQ-NEGATIVE", qty=-3, image="https://example/x.jpg")
    after = await counts()

    assert after["no_image"] == before["no_image"] + 1
    assert after["negative_stock"] == before["negative_stock"] + 1


async def test_informational_tiles_do_not_raise_the_dashboard_count(
    admin_client, _test_engine
) -> None:
    """The dashboard shows how many defect CLASSES need action. Archived and
    在庫管理対象外 SKUs are outcomes of deliberate cleanup, so counting them
    would leave a tidy system looking permanently broken."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    from app.services.data_quality import summary_tiles

    async with factory() as session:
        tiles = await summary_tiles(session, placeholder_pattern=r"^r-sku\d+$")

    informational = {t.key for t in tiles if t.informational}
    assert informational == {"unmanaged", "archived", "bundles"}

    r = await admin_client.get("/admin/", headers=_auth_header())
    assert r.status_code == 200
    assert "データ品質" in r.text


# --- Rakuten report ------------------------------------------------------


async def test_placeholder_skus_are_detected(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    good = await _seed_sku(factory, "RK-GOOD", qty=1)
    bad = await _seed_sku(factory, "RK-PLACEHOLDER", qty=1)
    await _seed_rakuten_mapping(factory, good, "N23gold")
    await _seed_rakuten_mapping(factory, bad, "r-sku00000042")

    r = await admin_client.get("/admin/data-quality/rakuten", headers=_auth_header())
    assert r.status_code == 200
    assert "r-sku00000042" in r.text
    # A real SKU code must not be flagged as a placeholder.
    section = r.text[: r.text.index("2. 重複するSKU管理番号")]
    assert "N23gold" not in section


async def test_duplicate_channel_skus_are_detected(admin_client, _test_engine) -> None:
    """One SKU管理番号 pointing at two masters means whichever mapping wins is
    arbitrary, so orders land against the wrong product silently.

    The marketplace_ids must differ: `uq_channel_sku_mapping` covers
    (channel, channel_sku, marketplace_id), so the database already prevents
    this within a single shop. The cross-shop collision is what is left to
    detect, and it is the one nothing else guards.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    a = await _seed_sku(factory, "DUP-A", qty=1)
    b = await _seed_sku(factory, "DUP-B", qty=1)
    await _seed_rakuten_mapping(factory, a, "shared-sku-1", marketplace_id="shop-1")
    await _seed_rakuten_mapping(factory, b, "shared-sku-1", marketplace_id="shop-2")

    r = await admin_client.get("/admin/data-quality/rakuten", headers=_auth_header())
    assert "shared-sku-1" in r.text
    assert "DUP-A" in r.text and "DUP-B" in r.text


async def test_unmapped_rakuten_sales_are_quantified(admin_client, _test_engine) -> None:
    """The money figure is the point: it is the precision limit on every later
    sales analytic, and the argument for getting RMS fixed."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_unmapped_rakuten_line(factory, "RK-UNMAPPED-1", "ghost-sku", 3, "1500")

    async with factory() as session:
        from app.services.data_quality import rakuten_sku_report

        report = await rakuten_sku_report(session, placeholder_pattern=r"^r-sku\d+$")

    assert report.unmapped_lines >= 1
    assert report.unmapped_quantity >= 3
    assert report.unmapped_amount >= Decimal("4500")

    r = await admin_client.get("/admin/data-quality/rakuten", headers=_auth_header())
    assert "ghost-sku" in r.text


async def test_report_csv_opens_in_excel_and_carries_all_three_sections(
    admin_client, _test_engine
) -> None:
    """The CSV IS the RMS correction request — it is opened in Excel on Windows,
    and a mojibake'd request gets ignored.

    It used to be CP932, which decodes only on a Japanese-locale Windows; on an
    English-locale machine 仮値 came out as `‰¼'l`. UTF-8 with a BOM is the one
    encoding Excel reads correctly everywhere.
    """
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    bad = await _seed_sku(factory, "CSV-RK", qty=1)
    await _seed_rakuten_mapping(factory, bad, "r-sku00000099")
    await _seed_unmapped_rakuten_line(factory, "RK-CSV-1", "csv-ghost", 2, "800")

    r = await admin_client.get("/admin/data-quality/rakuten/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf"), "Excel needs the BOM to pick UTF-8"
    text = r.content.decode("utf-8-sig")
    assert "区分,SKU管理番号" in text
    assert "仮値" in text and "r-sku00000099" in text
    assert "未マッピング実績" in text and "csv-ghost" in text


async def test_placeholder_pattern_is_configurable(admin_client, _test_engine) -> None:
    """RMS numbering has changed before; the pattern is a setting so the next
    change does not need a code deploy."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku = await _seed_sku(factory, "PAT-1", qty=1)
    await _seed_rakuten_mapping(factory, sku, "TEMP-0001")

    async with factory() as session:
        from app.services.data_quality import rakuten_sku_report

        default = await rakuten_sku_report(session, placeholder_pattern=r"^r-sku\d+$")
        custom = await rakuten_sku_report(session, placeholder_pattern=r"^TEMP-\d+$")

    assert not any(p["channel_sku"] == "TEMP-0001" for p in default.placeholder)
    assert any(p["channel_sku"] == "TEMP-0001" for p in custom.placeholder)


async def test_every_csv_export_is_utf8_with_a_bom(admin_client, _test_engine) -> None:
    """Excel reads a BOM-less CSV in the system codepage, so Japanese headers
    mojibake. Asserted across ALL exports, not one, because the 2026-08-29
    report came from two different endpoints failing in two different ways.
    """
    exports = [
        "/admin/inventory/export.csv",
        "/admin/categories/export.csv",
        "/admin/mappings/export.csv",
        "/admin/data-quality/rakuten/export.csv",
    ]
    for url in exports:
        r = await admin_client.get(url, headers=_auth_header())
        assert r.status_code == 200, f"{url} returned {r.status_code}"
        assert r.content.startswith(b"\xef\xbb\xbf"), f"{url} has no UTF-8 BOM"
        assert "charset=utf-8" in r.headers["content-type"], url
        # Decodes cleanly as UTF-8; a CP932 payload would raise here.
        r.content.decode("utf-8-sig")

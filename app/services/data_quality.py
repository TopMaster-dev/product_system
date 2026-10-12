"""Data-quality measurement — one place that counts what is wrong.

Two screens read from here: the Rakuten SKU defect report (which doubles as the
RMS correction request the client hands to their own team) and the data-quality
summary that tiles every defect class with a link to where it gets fixed.

Three rules hold across everything in this module.

**Every count is scoped by `sku_scope`.** A tile that counts the ~355 masters the
archive cleanup deliberately retired sends the client hunting for work that does
not exist, and a tile that counts gift boxes as "画像なし" is asking them to
photograph a coupon. Counting and the screen you land on must agree about the
population, or the tile is a lie with a link attached.

**Nothing here writes.** These are diagnostics; the fixes live on the screens
each tile links to.

**The expensive check is opt-in.** `snapshot != SUM(events)` is a full join over
`inventory_events`, which on db-f1-micro is not something a page load should do
on every visit. It runs only under `?deep=1`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
)
from app.services.categories import category_label_column, join_category
from app.services.sku_scope import operational_conditions

RAKUTEN = "rakuten"


@dataclass(frozen=True, slots=True)
class Tile:
    """One defect count, and where to go and fix it.

    `href` is not decoration: a number with nowhere to act on it becomes
    wallpaper. Every tile the summary renders must resolve, which
    tests/integration/test_data_quality_ui.py enforces by following them all.
    """

    key: str
    label: str
    count: int
    href: str
    hint: str = ""
    #: Non-zero is expected and fine (archived/unmanaged SKUs are outcomes of
    #: deliberate cleanup, not defects). Rendered muted rather than amber.
    informational: bool = False


@dataclass(slots=True)
class RakutenSkuReport:
    """The four ways a Rakuten SKU管理番号 fails to identify a product."""

    placeholder: list[dict[str, object]] = field(default_factory=list)
    duplicated: list[dict[str, object]] = field(default_factory=list)
    unmapped_lines: int = 0
    unmapped_quantity: int = 0
    unmapped_amount: Decimal = Decimal("0")
    unmapped_examples: list[dict[str, object]] = field(default_factory=list)

    @property
    def total_defects(self) -> int:
        return len(self.placeholder) + len(self.duplicated)


def placeholder_condition(pattern: str):  # type: ignore[no-untyped-def]
    """SQL predicate for an RMS auto-generated SKU (r-sku00000001 style).

    Postgres regex, so the pattern travels to the database rather than pulling
    every mapping into Python. Kept as a function because the pattern is a
    setting — RMS numbering has changed before.
    """
    return ChannelSkuMapping.channel_sku.op("~")(pattern)


async def rakuten_sku_report(
    session: AsyncSession,
    *,
    placeholder_pattern: str,
    example_limit: int = 200,
) -> RakutenSkuReport:
    """Everything wrong with the Rakuten SKU管理番号 set, in one pass.

    The placeholder and duplicate lists are what the client sends to RMS; the
    unmapped figures are what tells them why it matters, in money.
    """
    report = RakutenSkuReport()

    placeholder_rows = await session.execute(
        select(
            ChannelSkuMapping.channel_sku,
            ChannelSkuMapping.channel_product_id,
            MasterSku.sku_code,
            MasterSku.name,
        )
        .join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
        .where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
            placeholder_condition(placeholder_pattern),
        )
        .order_by(ChannelSkuMapping.channel_sku)
        .limit(example_limit)
    )
    report.placeholder = [
        {
            "channel_sku": sku,
            "channel_product_id": product_id or "",
            "master_sku_code": code,
            "product_name": name,
        }
        for sku, product_id, code, name in placeholder_rows.all()
    ]

    # One channel_sku pointing at several masters: whichever mapping wins is
    # arbitrary, so orders land against the wrong product silently.
    #
    # `uq_channel_sku_mapping` is on (channel, channel_sku, marketplace_id), so
    # the database already makes this impossible WITHIN one marketplace. What
    # survives to be detected here is the cross-marketplace collision: the same
    # SKU管理番号 reused for a different product in a second Rakuten shop, where
    # the lookup resolves by marketplace and a mis-tagged order silently picks
    # the wrong one.
    dup_subq = (
        select(ChannelSkuMapping.channel_sku)
        .where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
        )
        .group_by(ChannelSkuMapping.channel_sku)
        .having(func.count(func.distinct(ChannelSkuMapping.master_sku_id)) > 1)
        .subquery()
    )
    dup_rows = await session.execute(
        select(
            ChannelSkuMapping.channel_sku,
            func.count().label("mappings"),
            func.string_agg(MasterSku.sku_code, ", ").label("masters"),
        )
        .join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
        .where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
            ChannelSkuMapping.channel_sku.in_(select(dup_subq.c.channel_sku)),
        )
        .group_by(ChannelSkuMapping.channel_sku)
        .order_by(ChannelSkuMapping.channel_sku)
        .limit(example_limit)
    )
    report.duplicated = [
        {"channel_sku": sku, "mappings": count, "masters": masters}
        for sku, count, masters in dup_rows.all()
    ]

    # Rakuten order lines that never resolved to a master. These are the sales
    # that will be missing from every channel/category analytic, so the number
    # is also the precision limit on P2-011.
    unmapped = (
        await session.execute(
            select(
                func.count().label("lines"),
                func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"),
                func.coalesce(
                    func.sum(OrderItem.quantity * OrderItem.unit_price), Decimal("0")
                ).label("amount"),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.channel == RAKUTEN, OrderItem.master_sku_id.is_(None))
        )
    ).one()
    report.unmapped_lines = unmapped.lines or 0
    report.unmapped_quantity = unmapped.qty or 0
    report.unmapped_amount = unmapped.amount or Decimal("0")

    example_rows = await session.execute(
        select(
            OrderItem.channel_sku,
            func.count().label("lines"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"),
            func.coalesce(func.sum(OrderItem.quantity * OrderItem.unit_price), Decimal("0")).label(
                "amount"
            ),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == RAKUTEN, OrderItem.master_sku_id.is_(None))
        .group_by(OrderItem.channel_sku)
        .order_by(func.sum(OrderItem.quantity * OrderItem.unit_price).desc())
        .limit(example_limit)
    )
    report.unmapped_examples = [
        {
            "channel_sku": sku,
            "lines": lines,
            "quantity": qty,
            "amount": amount,
        }
        for sku, lines, qty, amount in example_rows.all()
    ]
    return report


def matches_placeholder(channel_sku: str, pattern: str) -> bool:
    """Python mirror of `placeholder_condition`, for CSV rendering and tests."""
    return re.match(pattern, channel_sku) is not None


@dataclass(frozen=True, slots=True)
class SkuListKind:
    """A tile whose defect is a set of SKUs, listed on its own page.

    These tiles used to link to the screen where the work is done — the CSV
    upload, the mapping list, the whole inventory — so the client landed on a
    screen that did not show WHICH products were affected, against the
    acceptance criterion 「件数と該当商品への導線」 (Q&A, 2026-10-12). The list
    and the tile count are built from the same `sku_list_conditions`, so the
    number on the tile is always the length of the list it opens.
    """

    key: str
    label: str
    hint: str
    #: Where the work itself is done, offered from the list. None when the fix
    #: is not an operation on this system (images come from Shopify).
    action_href: str | None = None
    action_label: str | None = None
    note: str = ""
    informational: bool = False


SKU_LIST_KINDS: dict[str, SkuListKind] = {
    k.key: k
    for k in (
        SkuListKind(
            "no_image",
            "画像なし",
            "Shopifyの商品画像が未取得のSKUです。",
            note="画像はShopifyの商品登録から自動で取り込みます。"
            "Shopifyに画像を登録したあと、技術担当が取り込みを実行します。",
        ),
        SkuListKind(
            "no_category",
            "カテゴリ未設定",
            "カテゴリ別分析では「未分類」に集約されます。",
            action_href="/admin/categories/upload",
            action_label="CSVでカテゴリを一括設定する",
            note="この一覧をCSVで出力し、category_code 列を追記して取り込むと、"
            "まとめて設定できます。",
        ),
        SkuListKind(
            "no_mapping",
            "マッピング欠落",
            "有効なチャネルマッピングが1件もないSKUです。受注しても紐づきません。",
            action_href="/admin/mappings/new",
            action_label="マッピングを追加する",
        ),
        SkuListKind(
            "unmanaged",
            "在庫管理対象外",
            "意図的な設定です (ギフトボックス等)。",
            action_href="/admin/inventory?include_hidden=1",
            action_label="在庫一覧で設定を変更する",
            informational=True,
        ),
        SkuListKind(
            "archived",
            "アーカイブ済",
            "移行で役目を終えた旧マスタです。過去の集計には引き続き含まれます。",
            action_href="/admin/inventory?include_hidden=1",
            action_label="在庫一覧で設定を変更する",
            informational=True,
        ),
        SkuListKind(
            "bundles",
            "セット商品 (親)",
            "在庫は構成品で管理し、販売できる数は構成品から自動計算します。",
            informational=True,
        ),
    )
}


def sku_list_href(key: str) -> str:
    return f"/admin/data-quality/skus?kind={key}"


def sku_list_conditions(key: str) -> list[ColumnElement[bool]]:
    """The population each SKU-list tile counts and its list shows."""
    operational = operational_conditions()
    if key == "no_image":
        return [MasterSku.image_url.is_(None), *operational]
    if key == "no_category":
        return [MasterSku.category_id.is_(None), *operational]
    if key == "no_mapping":
        # A SKU nothing can order: no active mapping on any channel.
        mapped_ids = select(ChannelSkuMapping.master_sku_id).where(
            ChannelSkuMapping.is_active.is_(True)
        )
        return [MasterSku.id.not_in(mapped_ids), *operational]
    if key == "unmanaged":
        return [MasterSku.is_stock_managed.is_(False), MasterSku.archived_at.is_(None)]
    if key == "archived":
        return [MasterSku.archived_at.is_not(None)]
    if key == "bundles":
        return [MasterSku.is_bundle.is_(True), MasterSku.archived_at.is_(None)]
    raise KeyError(key)


#: Rows the list page renders; the CSV carries every row.
SKU_LIST_LIMIT = 500


async def sku_list(
    session: AsyncSession, key: str, *, limit: int | None = SKU_LIST_LIMIT
) -> tuple[int, list[Any]]:
    """(total, rows) for one SKU-list tile. Rows are mappings with id, sku_code,
    name, category_name, is_stock_managed, archived_at, is_bundle."""
    conditions = sku_list_conditions(key)
    total = await _count(session, select(MasterSku.id).where(*conditions))
    stmt = (
        join_category(
            select(
                MasterSku.id,
                MasterSku.sku_code,
                MasterSku.name,
                MasterSku.is_stock_managed,
                MasterSku.archived_at,
                MasterSku.is_bundle,
                category_label_column(),
            ).select_from(MasterSku)
        )
        .where(*conditions)
        .order_by(MasterSku.sku_code)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).mappings().all()
    return total, list(rows)


async def _count(session: AsyncSession, stmt) -> int:  # type: ignore[no-untyped-def]
    return await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


async def summary_tiles(
    session: AsyncSession,
    *,
    placeholder_pattern: str,
    deep: bool = False,
) -> list[Tile]:
    """Every defect class, counted over the population the linked screen shows."""
    operational = operational_conditions()

    async def listed(key: str) -> int:
        return await _count(session, select(MasterSku.id).where(*sku_list_conditions(key)))

    no_image = await listed("no_image")
    no_category = await listed("no_category")
    no_mapping = await listed("no_mapping")
    negative = await _count(
        session,
        select(InventorySnapshot.master_sku_id)
        .join(MasterSku, MasterSku.id == InventorySnapshot.master_sku_id)
        .where(InventorySnapshot.on_hand_qty < 0, *operational),
    )
    placeholder_skus = await _count(
        session,
        select(ChannelSkuMapping.id).where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
            placeholder_condition(placeholder_pattern),
        ),
    )
    unmapped_sales = await _count(
        session,
        select(OrderItem.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.master_sku_id.is_(None)),
    )
    unmanaged = await listed("unmanaged")
    archived = await listed("archived")
    bundles = await listed("bundles")

    tiles = [
        Tile(
            "no_image",
            "画像なし",
            no_image,
            sku_list_href("no_image"),
            "Shopifyの商品画像が未取得。sync_shopify_images で補完できます。",
        ),
        Tile(
            "no_category",
            "カテゴリ未設定",
            no_category,
            sku_list_href("no_category"),
            "カテゴリ別分析では「未分類」に集約されます。",
        ),
        Tile(
            "no_mapping",
            "マッピング欠落",
            no_mapping,
            sku_list_href("no_mapping"),
            "有効なチャネルマッピングが1件もないSKU。受注しても紐づきません。",
        ),
        Tile(
            "negative_stock",
            "マイナス在庫",
            negative,
            "/admin/inventory?filter=negative",
            "実在庫はマイナスになりません。棚卸または手動調整で補正します。",
        ),
        Tile(
            "rakuten_placeholder",
            "仮値の楽天SKU",
            placeholder_skus,
            "/admin/data-quality/rakuten",
            "RMSの自動採番のままで、商品を識別できません。",
        ),
        Tile(
            "unmapped_sales",
            "未マッピング売上",
            unmapped_sales,
            "/admin/alerts",
            "マスターに紐づかない受注明細。売上分析から欠落します。",
        ),
        Tile(
            "unmanaged",
            "在庫管理対象外",
            unmanaged,
            sku_list_href("unmanaged"),
            "意図的な設定です (ギフトボックス等)。",
            informational=True,
        ),
        Tile(
            "archived",
            "アーカイブ済",
            archived,
            sku_list_href("archived"),
            "移行で役目を終えた旧マスタ。集計には引き続き含まれます。",
            informational=True,
        ),
        Tile(
            "bundles",
            "セット商品 (親)",
            bundles,
            sku_list_href("bundles"),
            "在庫は構成品から自動計算されます。",
            informational=True,
        ),
    ]

    if deep:
        tiles.append(
            Tile(
                "event_drift",
                "在庫イベント不整合",
                await count_event_drift(session),
                "/admin/events",
                "スナップショットとイベント合計の差。0 が正常です。",
            )
        )
    return tiles


async def count_event_drift(session: AsyncSession) -> int:
    """Snapshots whose quantity disagrees with the sum of their events.

    The invariant the whole event-sourced design rests on, and the most
    expensive question on this page — a full aggregate over `inventory_events`
    joined to every snapshot. Called only under `?deep=1`; running it on every
    page load would put a table scan on db-f1-micro in front of an operator
    who just wanted to see how many SKUs lack images.
    """
    totals = (
        select(
            InventoryEvent.master_sku_id.label("mid"),
            func.coalesce(func.sum(InventoryEvent.quantity_delta), 0).label("total"),
        )
        .group_by(InventoryEvent.master_sku_id)
        .subquery()
    )
    return (
        await session.scalar(
            select(func.count())
            .select_from(InventorySnapshot)
            .join(totals, totals.c.mid == InventorySnapshot.master_sku_id)
            .where(InventorySnapshot.on_hand_qty != totals.c.total)
        )
        or 0
    )


__all__ = [
    "RAKUTEN",
    "RakutenSkuReport",
    "Tile",
    "count_event_drift",
    "matches_placeholder",
    "placeholder_condition",
    "rakuten_sku_report",
    "summary_tiles",
]

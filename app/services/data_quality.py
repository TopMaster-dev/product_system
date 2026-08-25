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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
)
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

    no_image = await _count(
        session,
        select(MasterSku.id).where(MasterSku.image_url.is_(None), *operational),
    )
    no_category = await _count(
        session,
        select(MasterSku.id).where(MasterSku.category_id.is_(None), *operational),
    )
    # A SKU nothing can order: no active mapping on any channel.
    mapped_ids = select(ChannelSkuMapping.master_sku_id).where(
        ChannelSkuMapping.is_active.is_(True)
    )
    no_mapping = await _count(
        session,
        select(MasterSku.id).where(MasterSku.id.not_in(mapped_ids), *operational),
    )
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
    unmanaged = await _count(
        session,
        select(MasterSku.id).where(
            MasterSku.is_stock_managed.is_(False), MasterSku.archived_at.is_(None)
        ),
    )
    archived = await _count(session, select(MasterSku.id).where(MasterSku.archived_at.is_not(None)))
    bundles = await _count(
        session,
        select(MasterSku.id).where(MasterSku.is_bundle.is_(True), MasterSku.archived_at.is_(None)),
    )

    tiles = [
        Tile(
            "no_image",
            "画像なし",
            no_image,
            "/admin/inventory",
            "Shopifyの商品画像が未取得。sync_shopify_images で補完できます。",
        ),
        Tile(
            "no_category",
            "カテゴリ未設定",
            no_category,
            "/admin/categories/upload",
            "カテゴリ別分析では「未分類」に集約されます。",
        ),
        Tile(
            "no_mapping",
            "マッピング欠落",
            no_mapping,
            "/admin/mappings",
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
            "/admin/inventory?include_hidden=1",
            "意図的な設定です (ギフトボックス等)。",
            informational=True,
        ),
        Tile(
            "archived",
            "アーカイブ済",
            archived,
            "/admin/inventory?include_hidden=1",
            "移行で役目を終えた旧マスタ。集計には引き続き含まれます。",
            informational=True,
        ),
        Tile(
            "bundles",
            "セット商品 (親)",
            bundles,
            "/admin/inventory",
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

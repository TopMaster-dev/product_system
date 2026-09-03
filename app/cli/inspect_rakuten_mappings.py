"""Read-only report on the SHAPE of our Rakuten channel mappings.

Written to settle a question we could otherwise only infer from code: the 重複
defect check groups active rakuten mappings by channel_sku and keeps those with
more than one distinct master. But `uq_channel_sku_mapping` is on
(channel, channel_sku, marketplace_id) with NULLS NOT DISTINCT, so within one
marketplace that shape cannot exist — the check can only fire across
marketplaces, and nothing in this codebase ever sets marketplace_id for Rakuten.
If that holds in production too, "重複 0件" is not an all-clear; it is the only
answer the check is capable of giving, and we owe the client that distinction.

Also reports the inverse grouping (one master reachable through several Rakuten
keys), which is the 男性用/女性用 shared-page shape the client actually asked
about and which the current check cannot see by construction.

Writes nothing. Safe to run against production at any time.

Usage (via the Cloud SQL proxy):
    py -m app.cli.inspect_rakuten_mappings
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku, Order, OrderItem

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "rakuten"


async def collect(session: AsyncSession, *, placeholder_pattern: str) -> dict[str, Any]:
    mine = ChannelSkuMapping.channel == CHANNEL
    active = ChannelSkuMapping.is_active.is_(True)

    total = await session.scalar(select(func.count()).select_from(ChannelSkuMapping).where(mine))
    active_total = await session.scalar(
        select(func.count()).select_from(ChannelSkuMapping).where(mine, active)
    )
    # The decisive number: without a non-NULL marketplace_id the duplicate check
    # has nothing to group across.
    with_marketplace = await session.scalar(
        select(func.count())
        .select_from(ChannelSkuMapping)
        .where(mine, ChannelSkuMapping.marketplace_id.is_not(None))
    )
    distinct_sku = await session.scalar(
        select(func.count(func.distinct(ChannelSkuMapping.channel_sku))).where(mine, active)
    )

    # Exactly what rakuten_sku_report calls 重複.
    dup = (
        select(ChannelSkuMapping.channel_sku)
        .where(mine, active)
        .group_by(ChannelSkuMapping.channel_sku)
        .having(func.count(func.distinct(ChannelSkuMapping.master_sku_id)) > 1)
        .subquery()
    )
    duplicated = await session.scalar(select(func.count()).select_from(dup))

    # The inverse: one product reachable through several Rakuten keys. This is
    # the 男性用/女性用 shape, and it is legal — many-to-one is how shared stock
    # is meant to be expressed — so a non-zero count here is information, not a
    # defect. Reported because the 重複 check cannot see it.
    shared = (
        select(ChannelSkuMapping.master_sku_id)
        .where(mine, active)
        .group_by(ChannelSkuMapping.master_sku_id)
        .having(func.count(func.distinct(ChannelSkuMapping.channel_sku)) > 1)
        .subquery()
    )
    multi_key_masters = await session.scalar(select(func.count()).select_from(shared))

    placeholders = await session.scalar(
        select(func.count())
        .select_from(ChannelSkuMapping)
        .where(mine, active, ChannelSkuMapping.channel_sku.op("~")(placeholder_pattern))
    )
    # A purely numeric key is a 商品管理番号 (page level); the variant importer
    # writes SKU管理番号 instead. The split says which importer wrote each row,
    # and order lines can only ever match the numeric ones.
    numeric_keys = await session.scalar(
        select(func.count())
        .select_from(ChannelSkuMapping)
        .where(mine, active, ChannelSkuMapping.channel_sku.op("~")(r"^\d+$"))
    )

    # `channel` lives on Order, not OrderItem, so these MUST join or they count
    # Shopify's unmapped lines as Rakuten's.
    unmapped_lines = await session.scalar(
        select(func.count())
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == CHANNEL, OrderItem.master_sku_id.is_(None))
    )
    unmapped_keys = await session.scalar(
        select(func.count(func.distinct(OrderItem.channel_sku)))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == CHANNEL, OrderItem.master_sku_id.is_(None))
    )
    # Of those keys, the ones matching no active mapping at all. A key that DOES
    # match a mapping yet still has master_sku_id NULL was ingested before that
    # mapping existed: it needs a remap from us, not a new SKU from the client.
    orphan_keys = await session.scalar(
        select(func.count(func.distinct(OrderItem.channel_sku)))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.channel == CHANNEL,
            OrderItem.master_sku_id.is_(None),
            OrderItem.channel_sku.not_in(select(ChannelSkuMapping.channel_sku).where(mine, active)),
        )
    )
    masters = await session.scalar(select(func.count()).select_from(MasterSku))

    return {
        "mappings_total": total,
        "mappings_active": active_total,
        "with_marketplace_id": with_marketplace,
        "distinct_channel_sku": distinct_sku,
        "duplicated_channel_sku": duplicated,
        "masters_with_multiple_keys": multi_key_masters,
        "placeholder_keys": placeholders,
        "numeric_keys": numeric_keys,
        "unmapped_order_lines": unmapped_lines,
        "unmapped_distinct_keys": unmapped_keys,
        "unmapped_keys_with_no_mapping": orphan_keys,
        "master_skus": masters,
    }


async def run(*, session_factory: SessionFactory | None = None) -> int:
    factory = session_factory or async_session_factory
    pattern = get_settings().rakuten_placeholder_sku_pattern
    async with factory() as session:
        stats = await collect(session, placeholder_pattern=pattern)
    log.info("rakuten.mapping_shape", **stats)

    print("\n--- Rakuten マッピングの形状 (読み取りのみ) ---")
    print(f"  マッピング総数          : {stats['mappings_total']}")
    print(f"  うち有効                : {stats['mappings_active']}")
    print(f"  marketplace_id 設定あり : {stats['with_marketplace_id']}")
    print(f"  channel_sku ユニーク数  : {stats['distinct_channel_sku']}")
    print(f"  仮値パターン一致        : {stats['placeholder_keys']}")
    print(f"  数値キー (商品管理番号) : {stats['numeric_keys']}")
    print("\n  重複判定 (channel_sku -> 複数マスター)")
    print(f"    該当                  : {stats['duplicated_channel_sku']}")
    if not stats["with_marketplace_id"]:
        print("    ※ marketplace_id が全件 NULL のため、この判定は構造上 0 件しか返せません")
    print("\n  複数キー -> 単一マスター (男性用/女性用の形)")
    print(f"    該当マスター          : {stats['masters_with_multiple_keys']}")
    print("\n  未マッピング受注")
    print(f"    明細行                : {stats['unmapped_order_lines']}")
    print(f"    ユニークキー          : {stats['unmapped_distinct_keys']}")
    print(f"    うちマッピング皆無    : {stats['unmapped_keys_with_no_mapping']}")
    return 0


def main() -> None:
    argparse.ArgumentParser(description="Report Rakuten channel-mapping shape").parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()

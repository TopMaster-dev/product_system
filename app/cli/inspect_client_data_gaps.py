"""Read-only: what is still waiting on the client, with counts.

A request to a client is only actionable if it says how much. "Please finish
registering categories" is ignorable; "419 of 678 SKUs have no category" is a
task with an end. This produces the numbers that go into those messages, and it
is worth re-running before each one — asking for something already delivered
costs more credibility than asking late.

Read-only. Touches no channel and writes nothing.

    powershell -File scripts/run_cli.ps1 -Cli inspect_client_data_gaps
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku, ProductCategory
from app.services.data_quality import placeholder_condition
from app.services.sku_scope import analysable_conditions, operational_conditions

log = get_logger(__name__)


async def run() -> int:
    settings = get_settings()

    async with async_session_factory() as session:
        countable = await session.scalar(
            select(func.count())
            .select_from(MasterSku)
            .where(*analysable_conditions(include_archived=False))
        )
        operational = await session.scalar(
            select(func.count()).select_from(MasterSku).where(*operational_conditions())
        )
        categories = await session.scalar(select(func.count()).select_from(ProductCategory))
        roots = await session.scalar(
            select(func.count())
            .select_from(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
        )
        categorised = await session.scalar(
            select(func.count())
            .select_from(MasterSku)
            .where(*operational_conditions(), MasterSku.category_id.is_not(None))
        )
        placeholders = await session.scalar(
            select(func.count())
            .select_from(ChannelSkuMapping)
            .where(
                ChannelSkuMapping.channel == "rakuten",
                ChannelSkuMapping.is_active.is_(True),
                placeholder_condition(settings.rakuten_placeholder_sku_pattern),
            )
        )

    uncategorised = int(operational or 0) - int(categorised or 0)

    print("\n--- クライアント側の未整備データ ---\n")
    print(f"  棚卸の対象SKU数          {countable}")
    print("    分析対象の母集団。カウントシートの行数はこの数になります")
    print()
    print(f"  カテゴリ登録数            {categories}  (大分類 {roots})")
    print(f"  カテゴリ設定済みSKU        {categorised} / {operational}")
    print(f"  カテゴリ未設定SKU          {uncategorised}")
    print()
    print(f"  楽天の仮値SKU管理番号       {placeholders}")
    print("    r-sku 系の仮の番号。RMS側での修正が必要なもの")

    log.info(
        "client_gaps.done",
        countable=countable,
        categories=categories,
        categorised=categorised,
        uncategorised=uncategorised,
        rakuten_placeholders=placeholders,
    )
    return 0


def main() -> None:
    argparse.ArgumentParser(description="Report what is still waiting on the client").parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()

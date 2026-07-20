"""Deactivate legacy product-level channel mappings after the variant cutover.

After import_variant_mappings creates the variant-level masters + mappings, the
old Phase 1-A product-level masters (sku_code = CROSS MALL 商品コード) still carry
shopify/rakuten mappings that would resolve orders to the WRONG (product-level)
master. This deactivates them — but ONLY for products actually migrated (their
商品コード appears in a channel='crossmall' mapping), so any product never migrated
keeps working. The variant masters (crossmall targets) are never touched.

--dry-run reports what WOULD be deactivated without writing.

Usage (via the Cloud SQL proxy, after the importer):
    py -m app.cli.deactivate_legacy_mappings --dry-run
    py -m app.cli.deactivate_legacy_mappings
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli.import_variant_mappings import code_from_crossmall_key
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


async def legacy_mapping_ids(session: AsyncSession) -> list[int]:
    """Ids of active shopify/rakuten mappings on OLD product-level masters whose
    product was migrated to a variant master (its 商品コード appears in a crossmall
    mapping). Variant masters (crossmall targets) are excluded."""
    cm = await session.execute(
        select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.master_sku_id).where(
            ChannelSkuMapping.channel == "crossmall"
        )
    )
    migrated_codes: set[str] = set()
    variant_ids: set[int] = set()
    for channel_sku, master_id in cm.all():
        migrated_codes.add(code_from_crossmall_key(channel_sku))
        variant_ids.add(master_id)
    if not migrated_codes:
        return []

    old = await session.execute(select(MasterSku.id).where(MasterSku.sku_code.in_(migrated_codes)))
    old_ids = [mid for (mid,) in old.all() if mid not in variant_ids]
    if not old_ids:
        return []

    result = await session.execute(
        select(ChannelSkuMapping.id).where(
            ChannelSkuMapping.master_sku_id.in_(old_ids),
            ChannelSkuMapping.channel != "crossmall",
            ChannelSkuMapping.is_active.is_(True),
        )
    )
    return [mid for (mid,) in result.all()]


async def run(*, dry_run: bool, session_factory: SessionFactory | None = None) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        ids = await legacy_mapping_ids(session)
        if not dry_run and ids:
            await session.execute(
                update(ChannelSkuMapping)
                .where(ChannelSkuMapping.id.in_(ids))
                .values(is_active=False, updated_at=datetime.now(UTC))
            )
        log.info(
            "deactivate_legacy.done" if not dry_run else "deactivate_legacy.dry_run",
            matched=len(ids),
            deactivated=(len(ids) if not dry_run else 0),
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Deactivate legacy product-level channel mappings")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()

"""Zero the snapshots of legacy product-level masters after the variant cutover.

The cutover creates fresh variant masters (seeded from CROSS MALL) and
`deactivate_legacy_mappings` turns off the old product-level masters' channel
mappings — but those old masters KEEP their snapshots, which are often negative
from historical oversell, so they linger (and mislead) in the inventory list.

This emits a `stocktake` event zeroing each migrated legacy master's snapshot,
scoped EXACTLY like deactivate_legacy_mappings: only products whose 商品コード
appears in a channel='crossmall' mapping (i.e. actually migrated), and never the
variant masters (crossmall targets). Idempotent — one stocktake keyed
(stocktake, 'cutover', 'zero_legacy', sku_code, master_sku_id).

Run AFTER deactivate_legacy_mappings.

Usage (via the Cloud SQL proxy):
    py -m app.cli.zero_legacy_stock --dry-run
    py -m app.cli.zero_legacy_stock
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli.import_variant_mappings import code_from_crossmall_key
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

_SRC_CHANNEL = "cutover"
_SRC_ORDER = "zero_legacy"


async def legacy_master_ids(session: AsyncSession) -> list[int]:
    """Ids of OLD product-level masters whose 商品コード was migrated (appears in a
    crossmall mapping). Variant masters (crossmall targets) are excluded."""
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
    return [mid for (mid,) in old.all() if mid not in variant_ids]


async def run(
    *,
    dry_run: bool,
    all_negative: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        base = select(
            InventorySnapshot.master_sku_id,
            InventorySnapshot.on_hand_qty,
            MasterSku.sku_code,
        ).join(MasterSku, MasterSku.id == InventorySnapshot.master_sku_id)
        ids: list[int] = []
        if all_negative:
            # Clear EVERY remaining negative snapshot (retired duplicates,
            # non-inventory add-ons, real out-of-stock oversells) — physical
            # stock can't be negative, so the correct floor is 0.
            stmt = base.where(InventorySnapshot.on_hand_qty < 0)
        else:
            ids = await legacy_master_ids(session)
            stmt = base.where(
                InventorySnapshot.master_sku_id.in_(ids or [-1]),
                InventorySnapshot.on_hand_qty != 0,
            )
        rows = [(mid, qty, code) for mid, qty, code in (await session.execute(stmt)).all()]

        zeroed = 0
        now = datetime.now(UTC)
        for mid, on_hand, sku_code in rows:
            if dry_run:
                zeroed += 1
                continue
            event = InventoryEvent(
                master_sku_id=mid,
                event_type=InventoryEventTypeEnum.STOCKTAKE,
                quantity_delta=-on_hand,
                source_channel=_SRC_CHANNEL,
                source_order_id=_SRC_ORDER,
                source_line_id=sku_code,
                reason="Zero legacy/negative stock after variant cutover",
                occurred_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(event)
                    await session.flush()
            except IntegrityError:
                continue  # already zeroed on a prior run
            snapshot = (
                await session.execute(
                    select(InventorySnapshot)
                    .where(InventorySnapshot.master_sku_id == mid)
                    .with_for_update()
                )
            ).scalar_one()
            snapshot.on_hand_qty = 0
            snapshot.last_event_id = event.id
            zeroed += 1

        log.info(
            "zero_legacy.dry_run" if dry_run else "zero_legacy.done",
            mode="all_negative" if all_negative else "legacy",
            legacy_masters=len(ids),
            target_snapshots=len(rows),
            zeroed=zeroed,
            items=[f"{code}={qty}" for _, qty, code in rows][:60],
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Zero legacy/negative snapshots after cutover")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--all-negative",
        action="store_true",
        help="Zero EVERY negative snapshot (not just migrated legacy masters).",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, all_negative=args.all_negative)))


if __name__ == "__main__":
    main()

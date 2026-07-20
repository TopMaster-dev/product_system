"""One-off: seed per-variant initial stock for the variant-level masters.

Unlike seed_initial_stock (product-level 総在庫数量), this seeds each VARIANT
master from CROSS MALL's per-variant 在庫数量, aggregated by (token, color, size)
— matching how import_variant_mappings created the masters. `is_bundle` masters
(set parents + bracelets) hold NO own stock (availability is derived), so they
are skipped.

Idempotent — each seed emits one `receipt` event keyed by
(receipt, 'seed', 'variant_initial', sku_code, master_sku_id) under the widened
UNIQUE, so re-running does nothing.

Usage:
    py -m app.cli.seed_variant_stock --base csv_file/phase1-B/latest_version [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.cli.build_channel_mapping import (
    build_rakuten_index,
    load_crossmall,
    load_rakuten_rows,
    product_token,
)
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku

log = get_logger(__name__)
SEED_CHANNEL = "seed"
SEED_ORDER_ID = "variant_initial"


def aggregate_variant_stock(
    stock_map: dict[tuple[str, str, str], int],
    code2token: dict[str, str | None],
) -> dict[tuple[str, str, str], int]:
    """CROSS MALL stock keyed by (商品コード, color, size) -> keyed by
    (token, color, size), summed across the 商品コード that share a token
    (aliases like 006c/N23; in practice only one carries stock)."""
    out: dict[tuple[str, str, str], int] = defaultdict(int)
    for (code, color, size), qty in stock_map.items():
        token = code2token.get(code)
        if token:
            out[(token, color, size)] += qty
    return dict(out)


def clamp_negatives(
    variant_stock: dict[tuple[str, str, str], int],
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], int]]:
    """Physical stock can't be negative: return (clamped, negatives) where every
    qty < 0 in `clamped` is set to 0, and `negatives` records the original
    negative values (for review — CROSS MALL's own oversell/data issues)."""
    negatives = {k: v for k, v in variant_stock.items() if v < 0}
    clamped = {k: (v if v >= 0 else 0) for k, v in variant_stock.items()}
    return clamped, negatives


async def run(base: Path, reason: str, *, dry_run: bool, clamp_negative: bool = False) -> int:
    prod = Path(glob.glob(str(base / "item_[0-9]*.csv"))[0])
    skus = Path(glob.glob(str(base / "item_sku_*.csv"))[0])
    stock = Path(glob.glob(str(base / "stock_*.csv"))[0])
    rak = Path(glob.glob(str(base / "dl-normal-item_*.csv"))[0])

    xm_name, xm_var, stock_map = load_crossmall(prod, skus, stock)
    rk = build_rakuten_index(load_rakuten_rows(rak))
    code2token = {c: product_token(c, xm_name, rk) for c in xm_var}
    variant_stock = aggregate_variant_stock(stock_map, code2token)
    clamped_stock, negatives = clamp_negatives(variant_stock)
    if clamp_negative:
        variant_stock = clamped_stock

    log.info(
        "seed_variant.parsed",
        variant_keys=len(variant_stock),
        nonzero=sum(1 for v in variant_stock.values() if v != 0),
        negative=len(negatives),
        clamp_negative=clamp_negative,
    )
    if negatives:
        # Surface the negative CROSS MALL variants (up to 60) so the client can
        # review/fix them at source; with --clamp-negative these are seeded as 0.
        log.warning(
            "seed_variant.negatives",
            count=len(negatives),
            seeded_as="0 (clamped)" if clamp_negative else "as-is (negative!)",
            items=[f"{t}|{c}|{s}={v}" for (t, c, s), v in sorted(negatives.items())][:60],
        )
    if dry_run:
        log.info("seed_variant.dry_run")
        return 0

    seeded = skipped_existing = skipped_bundle = no_stock = 0
    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        result = await session.execute(
            select(MasterSku.id, MasterSku.sku_code, MasterSku.attributes, MasterSku.is_bundle)
        )
        for mid, sku_code, attrs, is_bundle in result.all():
            if is_bundle:
                skipped_bundle += 1
                continue
            key = (attrs.get("token", ""), attrs.get("color", ""), attrs.get("size", ""))
            qty = variant_stock.get(key, 0)
            if qty == 0:
                no_stock += 1
                continue
            event = InventoryEvent(
                master_sku_id=mid,
                event_type=InventoryEventTypeEnum.RECEIPT,
                quantity_delta=qty,
                source_channel=SEED_CHANNEL,
                source_order_id=SEED_ORDER_ID,
                source_line_id=sku_code,
                reason=reason,
                occurred_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(event)
                    await session.flush()
            except IntegrityError:
                skipped_existing += 1
                continue
            await session.execute(
                pg_insert(InventorySnapshot)
                .values(master_sku_id=mid, on_hand_qty=qty, last_event_id=event.id)
                .on_conflict_do_update(
                    index_elements=[InventorySnapshot.master_sku_id],
                    set_={
                        "on_hand_qty": InventorySnapshot.__table__.c.on_hand_qty + qty,
                        "last_event_id": event.id,
                        "updated_at": now,
                    },
                )
            )
            seeded += 1

    log.info(
        "seed_variant.done",
        seeded=seeded,
        skipped_existing=skipped_existing,
        skipped_bundle=skipped_bundle,
        no_stock=no_stock,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Seed per-variant initial stock")
    p.add_argument("--base", type=Path, default=Path("csv_file/phase1-B/latest_version"))
    p.add_argument("--reason", type=str, default="Variant initial stock seed from CROSS MALL")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--clamp-negative",
        action="store_true",
        help="Seed negative CROSS MALL variants as 0 (physical stock can't be negative).",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(args.base, args.reason, dry_run=args.dry_run, clamp_negative=args.clamp_negative)
        )
    )


if __name__ == "__main__":
    main()

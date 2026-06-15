"""One-off: seed initial stock per master_sku from CROSS MALL's 総在庫数量.

Idempotent — each seed emits one `receipt` inventory_event keyed by
(receipt, 'seed', 'initial', sku_code) under the existing UNIQUE constraint,
so re-running this script does nothing.

Usage:
    py -m app.cli.seed_initial_stock \\
        --products csv_file/sku/item_0601004857_000419.csv \\
        [--reason "Initial stock seed from CROSS MALL 2026-06-01"] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku

log = get_logger(__name__)
ENC = "cp932"
SEED_CHANNEL = "seed"
SEED_ORDER_ID = "initial"


def load_stock(path: Path) -> dict[str, int]:
    """Return {sku_code: 総在庫数量} from CROSS MALL product CSV."""
    with path.open("r", encoding=ENC, newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    code_idx = header.index("商品コード")
    qty_idx = header.index("総在庫数量")
    out: dict[str, int] = {}
    for r in rows[1:]:
        if len(r) <= max(code_idx, qty_idx):
            continue
        code = r[code_idx]
        raw = r[qty_idx]
        if not code or raw == "":
            continue
        try:
            qty = int(raw)
        except ValueError:
            log.warning("seed.skip_unparseable", code=code, raw=raw)
            continue
        # First occurrence wins (CSV may repeat the same code across rows).
        out.setdefault(code, qty)
    return out


async def run(products_path: Path, reason: str, *, dry_run: bool = False) -> int:
    log.info("seed.start", products=str(products_path), dry_run=dry_run, reason=reason)
    stock_map = load_stock(products_path)
    nonzero = {k: v for k, v in stock_map.items() if v != 0}
    log.info(
        "seed.parsed",
        total=len(stock_map),
        nonzero=len(nonzero),
        zero=len(stock_map) - len(nonzero),
        positive=sum(1 for v in stock_map.values() if v > 0),
        negative=sum(1 for v in stock_map.values() if v < 0),
    )

    if dry_run:
        return 0

    inserted = 0
    skipped_existing = 0
    skipped_no_sku = 0
    snapshot_applied = 0
    now = datetime.now(UTC)

    async with async_session_factory() as session, session.begin():
        # Fetch master_sku id by code.
        result = await session.execute(select(MasterSku.id, MasterSku.sku_code))
        code_to_id: dict[str, int] = {code: mid for mid, code in result.all()}

        for code, qty in stock_map.items():
            if qty == 0:
                continue
            sku_id = code_to_id.get(code)
            if sku_id is None:
                skipped_no_sku += 1
                continue

            event = InventoryEvent(
                master_sku_id=sku_id,
                event_type=InventoryEventTypeEnum.RECEIPT,
                quantity_delta=qty,
                source_channel=SEED_CHANNEL,
                source_order_id=SEED_ORDER_ID,
                source_line_id=code,
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

            # Upsert snapshot with += qty.
            stmt = (
                pg_insert(InventorySnapshot)
                .values(
                    master_sku_id=sku_id,
                    on_hand_qty=qty,
                    last_event_id=event.id,
                )
                .on_conflict_do_update(
                    index_elements=[InventorySnapshot.master_sku_id],
                    set_={
                        "on_hand_qty": InventorySnapshot.__table__.c.on_hand_qty + qty,
                        "last_event_id": event.id,
                        "updated_at": now,
                    },
                )
            )
            await session.execute(stmt)
            inserted += 1
            snapshot_applied += 1

    log.info(
        "seed.done",
        events_inserted=inserted,
        skipped_existing=skipped_existing,
        skipped_no_master_sku=skipped_no_sku,
        snapshots_applied=snapshot_applied,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed initial stock from CROSS MALL CSV")
    parser.add_argument("--products", required=True, type=Path)
    parser.add_argument("--reason", type=str, default="Initial stock seed from CROSS MALL")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(args.products, args.reason, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()

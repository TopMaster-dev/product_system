"""Flag 在庫管理対象外 masters from a CSV, and zero the damage they accumulated.

Some masters are not merchandise: gift boxes, coupons, made-to-order items,
"length adjustment" options. They are mapped and they appear on real orders, so
Phase 1-B consumed stock for them on every single order. Nobody ever restocks a
coupon, so their snapshots marched steadily negative — one reached **-12509** —
and they topped the best-seller ranking, which is how a gift box came to look
like the shop's most popular product.

This CLI does the two halves of the cleanup that must happen together:

1. Sets ``is_stock_managed = false`` and ``non_inventory_kind = <kind>``. From
   that moment ``InventoryService.resolve_consumption()`` returns an empty list
   for the master, so neither the consume path nor the cancellation-compensation
   path writes an event for it.
2. Emits ONE ``stocktake`` event per flagged master to bring the accumulated
   negative snapshot back to 0, keyed
   ``(stocktake, 'cleanup', 'mark_non_inventory', sku_code, master_sku_id)`` so a
   second run is a no-op rather than a second correction.

**Deployment order matters.** The image carrying the ``resolve_consumption``
guard must be live BEFORE this runs. Flag first and the next order re-opens the
hole; and because the guard is not retroactive, an order consumed before the flag
is not compensated when it is cancelled after — those few units stay consumed,
which is why the zeroing stocktake in step 2 exists.

CSV: two columns, ``sku_code`` and ``kind`` (aliases 種別 / 分類 accepted).
``kind`` is free text used for reporting — packaging / coupon / made_to_order /
option are the conventions.

Usage (via the Cloud SQL proxy):
    py -m app.cli.mark_non_inventory --csv non_inventory.csv --dry-run
    py -m app.cli.mark_non_inventory --csv non_inventory.csv
    py -m app.cli.mark_non_inventory --csv non_inventory.csv --unmark
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)
from app.ui.csv_intake import ColumnSpec, CsvSpec, iter_rows

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

_SRC_CHANNEL = "cleanup"
_SRC_ORDER = "mark_non_inventory"

NON_INVENTORY_CSV = CsvSpec(
    columns=(
        ColumnSpec(canonical="sku_code", aliases=("SKUコード", "SKU", "商品コード")),
        ColumnSpec(canonical="kind", aliases=("種別", "分類")),
    )
)


@dataclass(slots=True)
class MarkResult:
    requested: int = 0
    flagged: int = 0
    already: int = 0
    zeroed: int = 0
    zeroed_qty: int = 0
    not_found: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


def read_csv(path: pathlib.Path) -> dict[str, str]:
    """sku_code -> kind. Later rows win, so a corrected re-send is honoured."""
    data = path.read_bytes()
    return {row["sku_code"]: row["kind"] for row in iter_rows(data, NON_INVENTORY_CSV)}


async def run(
    *,
    csv_path: pathlib.Path,
    dry_run: bool,
    unmark: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    wanted = read_csv(csv_path)
    result = MarkResult(requested=len(wanted))
    factory = session_factory or async_session_factory

    async with factory() as session, session.begin():
        rows = (
            await session.execute(select(MasterSku).where(MasterSku.sku_code.in_(wanted or [""])))
        ).scalars()
        found = {sku.sku_code: sku for sku in rows}
        result.not_found = sorted(set(wanted) - set(found))

        now = datetime.now(UTC)
        for sku_code, kind in sorted(wanted.items()):
            master = found.get(sku_code)
            if master is None:
                continue
            target_managed = unmark
            if master.is_stock_managed == target_managed:
                result.already += 1
                continue
            result.flagged += 1
            result.items.append(f"{sku_code}({kind})")
            if dry_run:
                # Still report what the stocktake WOULD zero, so the reviewer
                # sees the full effect before agreeing to it.
                if not unmark:
                    qty = await _snapshot_qty(session, master.id)
                    if qty != 0:
                        result.zeroed += 1
                        result.zeroed_qty += qty
                continue

            # The CHECK constraint keeps flag and kind in lockstep; set both.
            master.is_stock_managed = target_managed
            master.non_inventory_kind = None if unmark else kind
            if unmark:
                continue
            if await _zero_snapshot(session, master, now):
                result.zeroed += 1

        log.info(
            "mark_non_inventory.dry_run" if dry_run else "mark_non_inventory.done",
            mode="unmark" if unmark else "mark",
            requested=result.requested,
            flagged=result.flagged,
            already_in_state=result.already,
            zeroed=result.zeroed,
            not_found=result.not_found[:30],
            items=result.items[:60],
        )
    return 0 if not result.not_found else 1


async def _snapshot_qty(session: AsyncSession, master_sku_id: int) -> int:
    qty = await session.scalar(
        select(InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id == master_sku_id
        )
    )
    return qty or 0


async def _zero_snapshot(session: AsyncSession, master: MasterSku, now: datetime) -> bool:
    """Bring the accumulated (usually negative) snapshot back to 0.

    The event carries a FIXED source key, so re-running the CLI hits the UNIQUE
    constraint and skips instead of correcting twice. Uses a SAVEPOINT for the
    same reason `zero_legacy_stock` does: an IntegrityError would otherwise
    poison the surrounding transaction and abort every remaining master.
    """
    snapshot = (
        await session.execute(
            select(InventorySnapshot)
            .where(InventorySnapshot.master_sku_id == master.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if snapshot is None or snapshot.on_hand_qty == 0:
        return False
    event = InventoryEvent(
        master_sku_id=master.id,
        event_type=InventoryEventTypeEnum.STOCKTAKE,
        quantity_delta=-snapshot.on_hand_qty,
        source_channel=_SRC_CHANNEL,
        source_order_id=_SRC_ORDER,
        source_line_id=master.sku_code,
        reason="在庫管理対象外に設定: 累積した見かけ上の在庫をゼロ化",
        occurred_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        return False  # already zeroed on a prior run
    snapshot.on_hand_qty = 0
    snapshot.last_event_id = event.id
    return True


def main() -> None:
    p = argparse.ArgumentParser(description="Flag 在庫管理対象外 masters from a CSV")
    p.add_argument("--csv", required=True, type=pathlib.Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--unmark",
        action="store_true",
        help="Reverse the flag (does NOT undo the zeroing stocktake).",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(csv_path=args.csv, dry_run=args.dry_run, unmark=args.unmark)))


if __name__ == "__main__":
    main()

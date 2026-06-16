"""Recompute inventory_snapshots from the inventory_events log (Phase 1-B).

`inventory_snapshots` is a materialized projection: `on_hand_qty` must equal
``SUM(quantity_delta)`` over `inventory_events` for each master_sku, and
`last_event_id` the latest event id (see the invariants in
`app/services/inventory.py`). This CLI rebuilds that projection from the
immutable event log.

Two uses:
- Drift detection: ``--dry-run`` reports any SKU whose snapshot disagrees
  with the event log, without writing.
- Repair / verification: rebuild the snapshot after a forward-fix
  (e.g. confirming a reconcile-verification rollback landed, docs/13
  §F1.7 / §F1.8). Safe and idempotent — it only ever sets the snapshot to
  the sum of the events.

Usage (via cloud-sql-proxy):
    py -m app.cli.recompute_snapshots --master-sku-id 42 [--dry-run]
    py -m app.cli.recompute_snapshots --all [--dry-run]

Exit codes:
  0 ok | 2 usage error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventoryEvent, InventorySnapshot

log = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 2

SessionFactory = async_sessionmaker[AsyncSession]


async def _target_sku_ids(session: AsyncSession, explicit: list[int] | None) -> list[int]:
    """Resolve the SKUs to recompute. Explicit list, or the union of every
    SKU that has a snapshot or any event (for --all)."""
    if explicit:
        return explicit
    snap_rows = await session.execute(select(InventorySnapshot.master_sku_id))
    event_rows = await session.execute(select(InventoryEvent.master_sku_id).distinct())
    ids = {r[0] for r in snap_rows.all()} | {r[0] for r in event_rows.all()}
    return sorted(ids)


async def recompute(
    session: AsyncSession,
    master_sku_ids: list[int],
    *,
    dry_run: bool,
) -> list[dict[str, object]]:
    """Recompute each SKU's snapshot from its events. Returns one result dict
    per SKU describing old vs computed values and whether it was updated."""
    results: list[dict[str, object]] = []
    for sku_id in master_sku_ids:
        agg = await session.execute(
            select(
                func.coalesce(func.sum(InventoryEvent.quantity_delta), 0),
                func.max(InventoryEvent.id),
            ).where(InventoryEvent.master_sku_id == sku_id)
        )
        computed_qty_raw, computed_last = agg.one()
        computed_qty = int(computed_qty_raw)

        snapshot = (
            await session.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.master_sku_id == sku_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        old_qty = snapshot.on_hand_qty if snapshot is not None else None
        old_last = snapshot.last_event_id if snapshot is not None else None
        drift = old_qty != computed_qty or old_last != computed_last

        if drift and not dry_run:
            if snapshot is None:
                snapshot = InventorySnapshot(master_sku_id=sku_id, on_hand_qty=computed_qty)
                session.add(snapshot)
            snapshot.on_hand_qty = computed_qty
            snapshot.last_event_id = computed_last
            await session.flush()

        results.append(
            {
                "master_sku_id": sku_id,
                "old_on_hand": old_qty,
                "computed_on_hand": computed_qty,
                "old_last_event_id": old_last,
                "computed_last_event_id": computed_last,
                "drift": drift,
                "updated": bool(drift and not dry_run),
            }
        )
    return results


async def run(
    *,
    master_sku_ids: list[int] | None,
    all_skus: bool,
    dry_run: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    """Top-level entry. `session_factory` defaults to production; tests pass a
    factory bound to the test engine."""
    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        targets = await _target_sku_ids(session, None if all_skus else master_sku_ids)
        results = await recompute(session, targets, dry_run=dry_run)

    drift_count = sum(1 for r in results if r["drift"])
    updated_count = sum(1 for r in results if r["updated"])
    sys.stdout.write(
        json.dumps(
            {
                "checked": len(results),
                "drift": drift_count,
                "updated": updated_count,
                "dry_run": dry_run,
                "results": results,
            }
        )
        + "\n"
    )
    log.info(
        "recompute_snapshots.done",
        checked=len(results),
        drift=drift_count,
        updated=updated_count,
        dry_run=dry_run,
    )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="recompute_snapshots",
        description="Rebuild inventory_snapshots from inventory_events (drift check / repair).",
    )
    parser.add_argument(
        "--master-sku-id",
        action="append",
        type=int,
        dest="master_sku_ids",
        help="A master_sku id to recompute. Repeat for several.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_skus",
        help="Recompute every SKU that has a snapshot or events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report drift without writing.",
    )
    args = parser.parse_args(argv)
    if not args.all_skus and not args.master_sku_ids:
        parser.error("provide --all or at least one --master-sku-id")
    return args


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return asyncio.run(
        run(
            master_sku_ids=args.master_sku_ids,
            all_skus=args.all_skus,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())

"""Apply order lines whose SKU was unknown when the order arrived.

The live counterpart to `reresolve_order_items`. Both fill `master_sku_id` from
the current mappings; this one also moves stock, the way first ingestion would
have. Use it for lines whose stock has NOT been settled since — a mapping added
today for an order from this week.

**Do not use it on the historical backlog.** The stocktake and the reconcile
have already settled the shelf, and replaying months of consumption on top
subtracts the same goods twice. That is what `reresolve_order_items` is for.

It scans lines, not orders. An order's status is a summary; `master_sku_id IS
NULL` is the fact, and lines stranded by the old alert-resolution behaviour sit
on orders already marked 確定 where a status-based scan cannot see them.

Idempotent — the UNIQUE on (event_type, source_channel, source_order_id,
source_line_id) blocks duplicate consumption, so re-running is safe.

    powershell -File scripts/run_cli.ps1 -Cli reprocess_pending_orders ^
        -Args "--dry-run --channel rakuten"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.cli._report_unresolved import print_report
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services.mapping import reresolve_unmapped_lines

log = get_logger(__name__)


async def run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    channel: str | None = None,
) -> int:
    log.info("reprocess.start", dry_run=dry_run, limit=limit, channel=channel)

    async with async_session_factory() as session:
        outcome = await reresolve_unmapped_lines(
            session,
            apply_stock=True,
            channel=channel,
            limit=limit,
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    print_report(
        title="未解決明細の反映 — 在庫も更新します",
        outcome=outcome,
        dry_run=dry_run,
    )
    log.info(
        "reprocess.done",
        lines_filled=outcome.lines_filled,
        stock_events=outcome.stock_events,
        cancelled_skipped=outcome.cancelled_skipped,
        unmanaged_skipped=outcome.unmanaged_skipped,
        orders_settled=outcome.orders_settled,
        unresolved_skus=len(outcome.unresolved),
        dry_run=dry_run,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Resolve unmapped order lines and apply them to stock",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--channel", default=None, help="rakuten / shopify; default is every channel")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, limit=args.limit, channel=args.channel)))


if __name__ == "__main__":
    main()

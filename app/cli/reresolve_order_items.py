"""Fill in the master SKU on historical order lines (W7-5 / 既知問題#3).

Run this after the client corrects a 管理番号 on the RMS side and the old value
is mapped onto the right master — and after any bulk mapping import. It is the
only way the past becomes correct for チャネル別売上構成 (P2-011) and the
検収用レポート (P2-042): both read `order_items.master_sku_id`, and a NULL there
is revenue attributed to nothing.

IT DOES NOT TOUCH STOCK, AND THAT IS THE POINT

The physical stocktake and the daily reconcile have already settled what is on
the shelf. Replaying months of consumption on top of a counted shelf subtracts
the same goods a second time — every SKU in the backlog would silently drop by
its own sales history. So this pass writes no `inventory_events` at all.

For lines whose stock has NOT been settled since — a mapping added today, for
an order from this week — use `reprocess_pending_orders`, which applies them the
way first ingestion would have.

Rebuild the rollups afterwards, or the corrected attribution never reaches the
dashboards. `--rebuild-metrics` does it in the same run.

    powershell -File scripts/run_cli.ps1 -Cli reresolve_order_items ^
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
    rebuild_metrics: bool = False,
) -> int:
    log.info("reresolve.start", dry_run=dry_run, limit=limit, channel=channel)

    async with async_session_factory() as session:
        outcome = await reresolve_unmapped_lines(
            session,
            apply_stock=False,  # see the module docstring — never from here
            channel=channel,
            limit=limit,
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    print_report(
        title="受注明細の再解決 — 在庫は変更しません",
        outcome=outcome,
        dry_run=dry_run,
    )
    log.info(
        "reresolve.done",
        lines_filled=outcome.lines_filled,
        orders_settled=outcome.orders_settled,
        unresolved_skus=len(outcome.unresolved),
        dry_run=dry_run,
    )

    if dry_run or not outcome.lines_filled:
        if not dry_run:
            print("  売上の再集計は不要です — 変更がありません")
        return 0

    if not rebuild_metrics:
        print(
            "\n  ※ 売上の集計はまだ古いままです。全期間を再構築してください:\n"
            "     powershell -File scripts/run_cli.ps1 -Cli rebuild_daily_metrics "
            '-Args "--max-days 0"'
        )
        return 0

    print("\n  売上集計を再構築します...")
    # Imported here, not at module scope: the rebuild is an opt-in tail step,
    # and importing it eagerly drags the rollup machinery into every run.
    from app.cli import rebuild_daily_metrics

    # max_days=0 is "no limit" — the correction reaches back as far as the
    # oldest line that changed, and a fixed window would silently miss it.
    rebuilt = await rebuild_daily_metrics.run(max_days=0, job_name="reresolve_order_items")
    print(f"  {rebuilt.days_rebuilt}日分を再集計しました")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill master_sku_id on historical order lines (no stock events)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--channel", default=None, help="rakuten / shopify; default is every channel")
    p.add_argument(
        "--rebuild-metrics",
        action="store_true",
        help="rebuild the daily rollups over the full period afterwards",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                dry_run=args.dry_run,
                limit=args.limit,
                channel=args.channel,
                rebuild_metrics=args.rebuild_metrics,
            )
        )
    )


if __name__ == "__main__":
    main()

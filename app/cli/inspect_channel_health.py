"""Read-only: when did each channel last produce an order, and how many?

Written on 2026-09-19, when the Rakuten poll was found returning 401 from
`searchOrder` and the success log had no entries in the retention window. Logs
answer "did the job succeed"; they cannot answer "is data still arriving",
because a poll that succeeds and finds nothing looks identical to one that has
silently stopped seeing a channel.

Orders are the only thing that decrements stock. A channel that stops arriving
does not raise anything by itself — the stock simply stops falling, and every
downstream number drifts quietly. This is the query that catches it, and it is
worth running before any stocktake: it says how much of the gap between recorded
and counted stock is explained by ingestion having stopped.

Read-only. No writes, no channel calls.

    powershell -File scripts/run_cli.ps1 -Cli inspect_channel_health
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import Order, OrderItem
from app.services.timeframe import to_jst_date

log = get_logger(__name__)

#: A channel quieter than this has almost certainly stopped arriving rather than
#: gone quiet. Even the slowest channel here produces something most days.
STALE_HOURS = 24


async def run(*, days: int = 30) -> int:
    since = datetime.now(UTC) - timedelta(days=days)
    now = datetime.now(UTC)

    async with async_session_factory() as session:
        latest = await session.execute(
            select(
                Order.channel,
                func.max(Order.ordered_at),
                func.count(),
            ).group_by(Order.channel)
        )
        overall = {c: (last, n) for c, last, n in latest.all()}

        recent = await session.execute(
            select(
                Order.channel,
                func.count(),
                func.coalesce(func.sum(OrderItem.quantity), 0),
            )
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(Order.ordered_at >= since)
            .group_by(Order.channel)
        )
        window = {c: (orders, int(units)) for c, orders, units in recent.all()}

        # Per-day counts for the trailing window, so a channel that stopped mid
        # window is visible as a cliff rather than just a lower total.
        daily = await session.execute(
            select(Order.channel, Order.ordered_at).where(Order.ordered_at >= since)
        )
        by_day: dict[str, set[str]] = defaultdict(set)
        for channel, ordered_at in daily.all():
            by_day[channel].add(to_jst_date(ordered_at).isoformat())

    print(f"\n--- チャネル別 受注取込状況 (直近{days}日) ---\n")
    header = (
        f"  {'channel':<12} {'最終受注':<22} {'総件数':>8}"
        f" {'期間件数':>9} {'期間点数':>9} {'稼働日':>7}"
    )
    print(header)
    print("  " + "-" * 74)

    stale: list[str] = []
    for channel in sorted(overall):
        last, total = overall[channel]
        orders, units = window.get(channel, (0, 0))
        active_days = len(by_day.get(channel, set()))
        age_hours = (now - last).total_seconds() / 3600 if last else None
        flag = ""
        if age_hours is not None and age_hours > STALE_HOURS:
            flag = f"  <-- {age_hours / 24:.1f}日 停止"
            stale.append(channel)
        stamp = last.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if last else "-"
        print(
            f"  {channel:<12} {stamp:<22} {total:>8} {orders:>9} {units:>9} {active_days:>7}{flag}"
        )

    if stale:
        print(f"\n  ※ {', '.join(stale)} は{STALE_HOURS}時間以上受注が入っていません。")
        print("     取込が止まっている間、そのチャネルの販売分は在庫から引かれていません。")
        print("     棚卸の前に停止期間を確定してください。")
    else:
        print(f"\n  すべてのチャネルが{STALE_HOURS}時間以内に受注を取り込んでいます。")

    log.info("channel_health.done", channels=len(overall), stale=len(stale))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Report per-channel order ingestion health")
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(days=args.days)))


if __name__ == "__main__":
    main()

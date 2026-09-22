"""Re-ingest Rakuten orders for a past window, after an ingestion outage.

Written for the 2026-08-25 → 2026-09-22 gap, when the RMS licence key expired
and roughly 265 orders never reached the system. Their stock was never
decremented and their revenue never counted, so the numbers drift by exactly
that much until they are pulled in.

WHY NOT JUST A BIG --lookback-minutes

`poll_channels` asks `searchOrder` for one window and reads ONE page of at most
1000 order numbers. It does not paginate. For a ten-minute poll that is correct
and cheap; for a month it is a silent truncation — the call succeeds, returns a
full page, and the orders past it are simply never seen. Nothing in the output
would say so.

So this walks the period in chunks and, when a chunk comes back at the page
limit, says the chunk may be incomplete and tells the operator to narrow it.
Rakuten also caps how wide a `searchOrder` span may be; chunking stays under
that without having to know the exact limit.

SAFE TO RE-RUN. `OrderIngestService.ingest` looks the order up by
(channel, channel_order_id) and routes an existing one to the update path, and
inventory events carry `uq_inventory_event_source`. Re-running a chunk cannot
double-count stock.

    powershell -File scripts/run_cli.ps1 -Cli backfill_rakuten_orders -WithRakuten ^
        -Args "--since 2026-08-25 --until 2026-09-22 --dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta

from app.adapters import RakutenAdapter
from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services.ingest import OrderIngestService
from app.services.timeframe import JST

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: What `_search_order_numbers` asks for per page. A chunk returning exactly
#: this many has almost certainly been truncated, because the adapter never
#: requests page 2.
PAGE_LIMIT = 1000

#: Narrow enough that a normal day stays far under PAGE_LIMIT and well inside
#: any RMS span cap, wide enough that a month is a few dozen calls rather than
#: hundreds.
DEFAULT_CHUNK_DAYS = 3


def _jst_bounds(day: date) -> tuple[datetime, datetime]:
    """00:00:00 to 23:59:59.999999 JST, as UTC instants.

    The adapter formats its own JST strings for RMS, so what matters here is
    only that consecutive chunks abut exactly — a gap of one second between
    them would drop any order placed in it, and the whole point is not to lose
    orders."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=JST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def chunks(since: date, until: date, chunk_days: int) -> list[tuple[date, date]]:
    """Inclusive [since, until] split into spans of at most `chunk_days`."""
    out: list[tuple[date, date]] = []
    cursor = since
    while cursor <= until:
        end = min(cursor + timedelta(days=chunk_days - 1), until)
        out.append((cursor, end))
        cursor = end + timedelta(days=1)
    return out


async def run(
    *,
    since: date,
    until: date,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    dry_run: bool = False,
) -> int:
    settings = get_settings()
    if not settings.rakuten_service_secret or not settings.rakuten_license_key:
        print("楽天の認証情報が設定されていません。-WithRakuten を付けて実行してください。")
        return EXIT_USAGE
    if until < since:
        print("--until は --since 以降の日付を指定してください。")
        return EXIT_USAGE

    spans = chunks(since, until, chunk_days)
    mode = "確認のみ" if dry_run else "取込"
    print(f"\n--- 楽天受注 遡及{mode} {since} 〜 {until} ({len(spans)}分割) ---\n")

    total_found = 0
    total_ingested = 0
    suspect: list[str] = []

    adapter = RakutenAdapter(
        service_secret=settings.rakuten_service_secret,
        license_key=settings.rakuten_license_key,
        shop_url=settings.rakuten_shop_url or None,
    )
    try:
        for first, last in spans:
            start, _ = _jst_bounds(first)
            _, end = _jst_bounds(last)
            label = f"{first} 〜 {last}" if first != last else str(first)
            try:
                numbers = await adapter._search_order_numbers(start, end)
            except Exception as exc:
                print(f"  {label:<26} 検索失敗: {exc!r}")
                log.exception("rakuten_backfill.search_failed", first=str(first))
                return EXIT_FAILED

            found = len(numbers)
            total_found += found
            flag = ""
            if found >= PAGE_LIMIT:
                # The adapter reads page 1 only, so a full page means orders
                # beyond it were never returned and would be lost silently.
                flag = "  ← 1ページ上限。--chunk-days を小さくして再実行してください"
                suspect.append(label)

            if dry_run or not numbers:
                print(f"  {label:<26} {found:>5}件{flag}")
                continue

            orders = await adapter.fetch_orders(since=start, until=end)
            async with async_session_factory() as session, session.begin():
                service = OrderIngestService(session)
                for order in orders:
                    await service.ingest(order)
            total_ingested += len(orders)
            print(f"  {label:<26} {found:>5}件  取込 {len(orders):>5}件{flag}")
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close:
            await close()

    print(f"\n  検索件数 合計: {total_found}")
    if not dry_run:
        print(f"  取込件数 合計: {total_ingested}")
        print("\n  ※ 在庫と売上への反映には日次集計の再構築が必要です:")
        print(f"     py -m app.cli.rebuild_daily_metrics --from {since} --to {until}")
    if suspect:
        print(f"\n  ⚠ 次の区間は取りこぼしの可能性があります: {', '.join(suspect)}")

    log.info(
        "rakuten_backfill.done",
        found=total_found,
        ingested=total_ingested,
        chunks=len(spans),
        dry_run=dry_run,
    )
    return EXIT_OK


def main() -> None:
    p = argparse.ArgumentParser(description="Re-ingest Rakuten orders for a past window")
    p.add_argument("--since", required=True, help="JST date, YYYY-MM-DD")
    p.add_argument("--until", required=True, help="JST date, YYYY-MM-DD (inclusive)")
    p.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                since=date.fromisoformat(args.since),
                until=date.fromisoformat(args.until),
                chunk_days=max(1, args.chunk_days),
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()

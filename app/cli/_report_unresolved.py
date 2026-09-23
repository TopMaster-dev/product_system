"""Shared console report for the two re-resolution CLIs.

Both answer the same operator question — "what do I still have to map, and
which one first?" — so they print the same table, and the ranking is by damage
rather than alphabetical: the SKU with 40 lines behind it is worth more than
the next 39 put together.
"""

from __future__ import annotations

from app.services.mapping import ReResolution

_MAX_ROWS = 40


def print_report(*, title: str, outcome: ReResolution, dry_run: bool) -> None:
    print(f"\n  === {title} ===" + ("  (dry-run: 保存しません)" if dry_run else ""))
    print(f"  マスタSKUを補完した明細 {outcome.lines_filled:>5}件")
    if outcome.stock_events:
        print(f"  在庫を減らした件数      {outcome.stock_events:>5}件")
    if outcome.cancelled_skipped:
        print(f"  キャンセル済のため据置  {outcome.cancelled_skipped:>5}件")
    if outcome.unmanaged_skipped:
        print(f"  在庫管理対象外         {outcome.unmanaged_skipped:>5}件")
    print(f"  確定に更新した受注      {outcome.orders_settled:>5}件")

    if not outcome.unresolved:
        print("\n  マッピング待ちの明細はありません")
        return

    print(f"\n  マッピングが必要なSKU {len(outcome.unresolved)}件 — 影響の大きい順")
    ranked = sorted(outcome.unresolved.items(), key=lambda kv: kv[1].lines, reverse=True)
    for (channel, channel_sku), stat in ranked[:_MAX_ROWS]:
        span = ""
        if stat.first_ordered_at and stat.last_ordered_at:
            span = f"  {stat.first_ordered_at:%Y-%m-%d} 〜 {stat.last_ordered_at:%Y-%m-%d}"
        print(f"    {channel:<8} {channel_sku:<32} {stat.lines:>4}明細 {stat.units:>5}点{span}")
    if len(ranked) > _MAX_ROWS:
        # Said out loud: a truncated list that looks complete is how the tail
        # of the backlog stops being worked on.
        print(f"    ... ほか {len(ranked) - _MAX_ROWS}件")

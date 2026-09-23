"""検収用の突合シート: 画面の数値 vs 受注データ (P2-042).

Read-only. Prints the comparison and, with `--out`, writes it as a CSV to hand
to the client alongside the 指標定義書.

The daily table is the part that earns the sheet. A period total that agrees
proves the period; a period total that disagrees says nothing about WHERE, and
「9月のどこかで売上が4,200円ずれている」 is not something anyone can act on.
Per-day rows put the disagreement on a date, and a date is something you can
look up in the order list.

    powershell -File scripts/run_cli.ps1 -Cli verify_analytics_totals ^
        -Args "--days 7 --out csv_file/phase2/acceptance"

Exit codes: 0 は突合一致、1 は差異あり — 検収チェックから判定できるように。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services.analytics_audit import (
    Reconciliation,
    recompute_from_orders,
    reconcile_period,
)
from app.services.analytics_query import period_kpis
from app.services.timeframe import Period, to_jst_date

log = get_logger(__name__)

DEFAULT_DAYS = 7

CSV_HEADER = [
    "日付",
    "画面_売上金額",
    "受注データ_売上金額",
    "差異_売上金額",
    "画面_販売点数",
    "受注データ_販売点数",
    "画面_受注件数",
    "受注データ_受注件数",
    "画面_未マッピング売上",
    "受注データ_未マッピング売上",
    "キャンセル済売上",
]


@dataclass(frozen=True, slots=True)
class DayRow:
    """One JST day, from both paths."""

    day: date
    screen_sales: Decimal
    orders_sales: Decimal
    screen_quantity: int
    orders_quantity: int
    screen_order_count: int
    orders_order_count: int
    screen_unmapped: Decimal
    orders_unmapped: Decimal
    cancelled_sales: Decimal

    @property
    def sales_difference(self) -> Decimal:
        return self.screen_sales - self.orders_sales

    @property
    def agrees(self) -> bool:
        return (
            self.sales_difference == 0
            and self.screen_quantity == self.orders_quantity
            and self.screen_order_count == self.orders_order_count
        )

    def as_csv(self) -> list[object]:
        return [
            self.day.isoformat(),
            self.screen_sales,
            self.orders_sales,
            self.sales_difference,
            self.screen_quantity,
            self.orders_quantity,
            self.screen_order_count,
            self.orders_order_count,
            self.screen_unmapped,
            self.orders_unmapped,
            self.cancelled_sales,
        ]


def _yen(value: Decimal) -> str:
    return f"{value:,.0f}"


async def daily_rows(session: AsyncSession, period: Period) -> list[DayRow]:
    """Each day reconciled as its own one-day period — the same arithmetic the
    client does when they pick a date and add up that day's orders."""
    rows: list[DayRow] = []
    for offset in range(period.days):
        day = period.first_day + timedelta(days=offset)
        one = Period(day, day, "day")
        kpis = await period_kpis(session, one)
        raw = await recompute_from_orders(session, one)
        rows.append(
            DayRow(
                day=day,
                screen_sales=kpis.gross_sales_jpy,
                orders_sales=raw.gross_sales_jpy,
                screen_quantity=kpis.sold_quantity,
                orders_quantity=raw.sold_quantity,
                screen_order_count=kpis.order_count,
                orders_order_count=raw.order_count,
                screen_unmapped=kpis.unmapped_sales_jpy,
                orders_unmapped=raw.unmapped_sales_jpy,
                cancelled_sales=raw.cancelled_sales_jpy,
            )
        )
    return rows


def print_report(result: Reconciliation, daily: list[DayRow]) -> None:
    p = result.period
    print(f"\n  === 分析数値の突合  {p.first_day} 〜 {p.last_day}  {p.days}日間 ===")
    print("  「画面」= 日次ロールアップの合計 / 「受注データ」= 受注明細からの直接集計\n")

    print(f"    {'項目':<24}{'画面':>16}{'受注データ':>16}{'差異':>12}")
    for m in result.measures:
        mark = "" if m.agrees else "  ★"
        print(
            f"    {m.label:<24}{_yen(m.from_screen):>16}"
            f"{_yen(m.from_orders):>16}{_yen(m.difference):>12}{mark}"
        )

    if result.agrees:
        print("\n  すべて一致しました")
    else:
        print(f"\n  ★ {len(result.disagreements)}項目が一致しません")

    if result.missing_days:
        shown = ", ".join(d.isoformat() for d in result.missing_days[:10])
        more = f" ほか{len(result.missing_days) - 10}日" if len(result.missing_days) > 10 else ""
        print(f"\n  集計行が無い日: {len(result.missing_days)}日  {shown}{more}")
        print("  期間がシステム稼働前に及んでいるか、ロールアップが失敗しています。")
        print("  復旧: rebuild_daily_metrics で該当日を再構築してください。")

    raw = result.raw
    print("\n  --- 差異の要因になりうるもの ---")
    cancelled = f"{_yen(raw.cancelled_sales_jpy)} 円 / {raw.cancelled_quantity}点"
    print(f"    キャンセル済の売上    {cancelled:>20}")
    print("      過去の受注が後からキャンセルされると、その受注日の集計が変わります。")
    print("      再集計していない日があると、ここが差異として現れます。")
    print(f"    未マッピングの明細    {raw.unmapped_line_count:>14} 件")
    print(f"    未マッピングの売上    {_yen(raw.unmapped_sales_jpy):>14} 円")
    print("      商品を特定できていない売上です。売上金額には含めず別枠で計上しています。")
    print(f"    売上合計 (未マッピング込) {_yen(result.kpis.total_with_unmapped_jpy):>12} 円")

    print("\n  --- 日別 ---")
    print(
        f"    {'日付':<12}{'画面(売上)':>14}{'受注(売上)':>14}{'差異':>10}"
        f"{'画面(点数)':>12}{'受注(点数)':>12}"
    )
    for row in daily:
        mark = "" if row.agrees else "  ★"
        print(
            f"    {row.day.isoformat():<12}{_yen(row.screen_sales):>14}"
            f"{_yen(row.orders_sales):>14}{_yen(row.sales_difference):>10}"
            f"{row.screen_quantity:>12}{row.orders_quantity:>12}{mark}"
        )


def write_csv(out_dir: Path, period: Period, daily: list[DayRow]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"acceptance_totals_{period.first_day}_{period.last_day}.csv"
    body = csv_body(CSV_HEADER, [row.as_csv() for row in daily])
    path.write_text(UTF8_BOM + body, encoding="utf-8")
    return path


def whole_days_before_today(days: int, *, now: datetime | None = None) -> Period:
    """Whole JST days only, ending yesterday.

    Today is still accumulating and its rollup ran at best an hour ago, so
    including it produces a disagreement that means nothing and reappears on
    every run.
    """
    today = to_jst_date(now or datetime.now(UTC))
    return Period(today - timedelta(days=days), today - timedelta(days=1), "custom")


async def run(*, days: int = DEFAULT_DAYS, out_dir: Path | None = None) -> int:
    period = whole_days_before_today(days)

    async with async_session_factory() as session:
        result = await reconcile_period(session, period)
        daily = await daily_rows(session, period)

    print_report(result, daily)

    if out_dir is not None:
        print(f"\n  突合シート -> {write_csv(out_dir, period, daily)}")

    log.info(
        "analytics_audit.done",
        first_day=str(period.first_day),
        last_day=str(period.last_day),
        agrees=result.agrees,
        disagreements=[m.label for m in result.disagreements],
        missing_days=len(result.missing_days),
    )
    return 0 if result.agrees else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Reconcile the dashboard against the orders")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="whole JST days back from today")
    p.add_argument("--out", default=None, help="directory to write the CSV sheet into")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(days=args.days, out_dir=Path(args.out) if args.out else None)))


if __name__ == "__main__":
    main()

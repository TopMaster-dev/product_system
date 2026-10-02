"""Read-only: what the 検収 needs from production that no screen shows on its own.

1. **日別・チャネル別の受注集計.** The client will pick a day and add up their
   own 受注一覧 in RMS and in Shopify. The dashboard shows one total per
   period; this splits each day by channel so each 受注一覧 has its own number
   to match, with the cancelled orders and unmapped lines that explain a gap
   counted beside it. `--out` writes the same table as a CSV.

2. **金額の基準.** `unit_price` is Rakuten's `price` and Shopify's
   `originalUnitPriceSet`. Whether those include consumption tax is a shop
   setting, recorded on every line of the payloads we store, so it can be
   measured instead of assumed. Also counted: Rakuten lines flagged
   `deleteItemFlag` (removed from the order after it was placed), and how often
   an order's line total equals RMS's own `goodsPrice` (商品合計金額), which is
   the column the client should compare against.

3. **セット・共有在庫の実例.** A parent with recent sales, its components, and
   the stock events its latest order line actually wrote — the P2-042
   walkthrough, with the admin URLs to open.

PRIVACY: a Rakuten payload carries the purchaser's name, address and phone
number. This reads price and tax keys only and prints tallies. The single order
number printed in section 3 is the shop's own reference, so the client can
open that order in their 受注一覧 during the walkthrough.

    powershell -File scripts/run_cli.ps1 -Cli inspect_review_materials
    powershell -File scripts/run_cli.ps1 -Cli inspect_review_materials `
        -Args "--out csv_file/phase2/acceptance"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.cli.verify_analytics_totals import DEFAULT_DAYS, whole_days_before_today
from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    BundleComponent,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
)
from app.services.analytics_audit import CANCELLED_STATUSES
from app.services.timeframe import Period, jst_date_expr

log = get_logger(__name__)

PRICE_DAYS = 30
BUNDLE_DAYS = 14
#: Payloads are large; this is plenty to establish a shop-wide setting.
PAYLOAD_LIMIT = 3000

CSV_HEADER = [
    "日付",
    "チャネル",
    "受注件数",
    "販売点数",
    "売上金額",
    "未マッピング売上",
    "キャンセル・返品件数",
]


# --- 1. 日別・チャネル別 ---------------------------------------------------


@dataclass(slots=True)
class ChannelDay:
    day: date
    channel: str
    orders: int = 0
    quantity: int = 0
    sales: Decimal = Decimal(0)
    unmapped: Decimal = Decimal(0)
    cancelled_orders: int = 0

    def as_csv(self) -> list[object]:
        return [
            self.day.isoformat(),
            self.channel,
            self.orders,
            self.quantity,
            self.sales,
            self.unmapped,
            self.cancelled_orders,
        ]


async def channel_days(session: AsyncSession, period: Period) -> list[ChannelDay]:
    """The arithmetic of `analytics_audit.recompute_from_orders`, split by
    channel. Orders are counted from `orders`, not from lines, exactly as that
    function does — a three-line order is one order."""
    start, end = period.utc_bounds()
    in_period = (Order.ordered_at >= start, Order.ordered_at < end)
    cancelled = Order.status.in_(CANCELLED_STATUSES)
    live = ~cancelled
    mapped = OrderItem.master_sku_id.is_not(None)
    amount = OrderItem.quantity * OrderItem.unit_price

    rows: dict[tuple[date, str], ChannelDay] = {}

    def row(day: date, channel: str) -> ChannelDay:
        return rows.setdefault((day, channel), ChannelDay(day, channel))

    order_day = jst_date_expr(Order.ordered_at)
    counts = await session.execute(
        select(
            order_day,
            Order.channel,
            func.count().filter(live),
            func.count().filter(cancelled),
        )
        .where(*in_period)
        .group_by(order_day, Order.channel)
    )
    for day, channel, live_n, cancelled_n in counts.all():
        r = row(day, channel)
        r.orders, r.cancelled_orders = int(live_n), int(cancelled_n)

    line_day = jst_date_expr(Order.ordered_at)
    lines = await session.execute(
        select(
            line_day,
            Order.channel,
            func.coalesce(func.sum(OrderItem.quantity).filter(live, mapped), 0),
            func.coalesce(func.sum(amount).filter(live, mapped), 0),
            func.coalesce(func.sum(amount).filter(live, ~mapped), 0),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(*in_period)
        .group_by(line_day, Order.channel)
    )
    for day, channel, qty, sales, unmapped in lines.all():
        r = row(day, channel)
        r.quantity, r.sales, r.unmapped = int(qty), Decimal(sales), Decimal(unmapped)

    return [rows[k] for k in sorted(rows)]


def print_channel_days(period: Period, days: list[ChannelDay]) -> None:
    print(f"\n  === 1. 日別・チャネル別の受注集計  {period.first_day} 〜 {period.last_day} ===")
    print("  各チャネルの受注一覧と、この表の同じ日・同じチャネルの行を比べてください。\n")
    print(
        f"    {'日付':<12}{'チャネル':<10}{'受注件数':>8}{'販売点数':>8}"
        f"{'売上金額':>12}{'未マッピング':>12}{'キャンセル':>10}"
    )
    for d in days:
        print(
            f"    {d.day.isoformat():<12}{d.channel:<10}{d.orders:>8}{d.quantity:>8}"
            f"{d.sales:>12,.0f}{d.unmapped:>12,.0f}{d.cancelled_orders:>10}"
        )
    print(
        "\n  受注一覧と差が出たときの確認順:"
        "\n    - 受注日の区切りは日本時間 0:00 です"
        "\n    - キャンセル・返品の注文は売上・件数に含みません (右端の件数)"
        "\n    - 売上金額は「単価 x 数量」の合計です。送料・クーポン値引・ポイント利用は含みません"
        "\n    - 商品を特定できていない明細は売上金額に含めず「未マッピング」に計上しています"
    )


def write_csv(out_dir: Path, period: Period, days: list[ChannelDay]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"acceptance_channel_days_{period.first_day}_{period.last_day}.csv"
    path.write_text(UTF8_BOM + csv_body(CSV_HEADER, [d.as_csv() for d in days]), encoding="utf-8")
    return path


# --- 2. 金額の基準 ---------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None and value != "" else None
    except InvalidOperation:
        return None


@dataclass(slots=True)
class PriceBasis:
    orders: int = 0
    lines: int = 0
    include_tax_flag: Counter[str] = field(default_factory=Counter)
    #: Lines carrying `priceTaxIncl` at all, and of those, equal to `price`.
    with_tax_incl: int = 0
    price_is_tax_incl: int = 0
    deleted_lines: int = 0
    deleted_amount: Decimal = Decimal(0)
    with_goods_price: int = 0
    goods_price_matches: int = 0
    with_postage: int = 0
    with_coupon: int = 0
    shopify_orders: int = 0
    shopify_taxes_included: Counter[str] = field(default_factory=Counter)

    def _add_rakuten_line(self, ln: dict[str, Any]) -> Decimal:
        """One ItemModel. Returns price * units, the amount we store."""
        self.lines += 1
        flag = ln.get("includeTaxFlag")
        self.include_tax_flag[str(flag) if flag is not None else "記録なし"] += 1
        price = _decimal(ln.get("price"))
        incl = _decimal(ln.get("priceTaxIncl"))
        if incl is not None:
            self.with_tax_incl += 1
            self.price_is_tax_incl += price == incl
        amount = (price or Decimal(0)) * int(ln.get("units") or 0)
        if str(ln.get("deleteItemFlag")) == "1":
            self.deleted_lines += 1
            self.deleted_amount += amount
        return amount

    def add_rakuten(self, payload: dict[str, Any]) -> None:
        """Reads price and tax keys only. Never a name, address or phone."""
        self.orders += 1
        line_total = sum(
            (
                self._add_rakuten_line(ln)
                for pkg in payload.get("PackageModelList") or []
                for ln in pkg.get("ItemModelList") or []
            ),
            Decimal(0),
        )
        goods = _decimal(payload.get("goodsPrice"))
        if goods is not None:
            self.with_goods_price += 1
            if goods == line_total:
                self.goods_price_matches += 1
        if (_decimal(payload.get("postagePrice")) or 0) > 0:
            self.with_postage += 1
        if (_decimal(payload.get("couponAllTotalPrice")) or 0) > 0:
            self.with_coupon += 1

    def add_shopify(self, payload: dict[str, Any]) -> None:
        self.shopify_orders += 1
        value = payload.get("taxes_included")
        self.shopify_taxes_included[str(value) if value is not None else "記録なし"] += 1

    @property
    def rakuten_verdict(self) -> str:
        if not self.lines:
            return "明細がありません"
        if self.include_tax_flag.get("1") == self.lines:
            return "税込 — 全明細が includeTaxFlag=1"
        if self.include_tax_flag.get("0") == self.lines:
            return "税別 — 全明細が includeTaxFlag=0"
        if self.with_tax_incl and self.price_is_tax_incl == self.with_tax_incl:
            return "税込 — price と priceTaxIncl が全明細で一致"
        return "混在または判定不能 — 内訳を確認してください"

    @property
    def shopify_verdict(self) -> str:
        known = self.shopify_taxes_included.get("True", 0) + self.shopify_taxes_included.get(
            "False", 0
        )
        if not known:
            return "記録がありません — ポーリング取込の注文には taxes_included が含まれません"
        if self.shopify_taxes_included.get("True") == known:
            return f"税込 — 記録のある{known}件すべて taxes_included=true"
        if self.shopify_taxes_included.get("False") == known:
            return f"税別 — 記録のある{known}件すべて taxes_included=false"
        return "混在 — 内訳を確認してください"


async def price_basis(session: AsyncSession, *, since: datetime) -> PriceBasis:
    basis = PriceBasis()
    rows = await session.execute(
        select(Order.channel, Order.raw_payload)
        .where(Order.ordered_at >= since, Order.raw_payload.is_not(None))
        .order_by(Order.ordered_at.desc())
        .limit(PAYLOAD_LIMIT)
    )
    for channel, payload in rows.all():
        if not isinstance(payload, dict):
            continue
        if channel == "rakuten":
            basis.add_rakuten(payload)
        elif channel == "shopify":
            basis.add_shopify(payload)
    return basis


def print_price_basis(basis: PriceBasis) -> None:
    print(f"\n  === 2. 金額の基準  直近{PRICE_DAYS}日の受注 ===")
    print(f"\n  楽天  {basis.orders}注文 / {basis.lines}明細")
    print(f"    単価の税区分: {basis.rakuten_verdict}")
    print(f"      includeTaxFlag の内訳: {dict(basis.include_tax_flag)}")
    print(f"      price = priceTaxIncl の明細: {basis.price_is_tax_incl} / {basis.with_tax_incl}")
    print(
        f"    明細合計が RMS の商品合計金額 (goodsPrice) と一致する注文: "
        f"{basis.goods_price_matches} / {basis.with_goods_price}"
    )
    print(f"    送料のある注文: {basis.with_postage}  クーポン利用のある注文: {basis.with_coupon}")
    print(
        "      送料・クーポン値引は売上金額に含めていません。"
        "受注一覧の請求金額とは、この分だけ差が出ます。"
    )
    if basis.deleted_lines:
        print(
            f"    ★ 注文後に削除された明細 (deleteItemFlag=1): {basis.deleted_lines}件 / "
            f"{basis.deleted_amount:,.0f}円 — 現在は売上に含まれています"
        )
    else:
        print("    注文後に削除された明細 (deleteItemFlag=1): 0件")
    print(f"\n  Shopify  {basis.shopify_orders}注文")
    print(f"    単価の税区分: {basis.shopify_verdict}")


# --- 3. セット・共有在庫 ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Component:
    master_sku_id: int
    sku_code: str
    name: str
    quantity_per: int
    on_hand: int
    shared_by: int


@dataclass(frozen=True, slots=True)
class ParentExample:
    master_sku_id: int
    sku_code: str
    name: str
    quantity: int
    sales: Decimal
    components: list[Component]
    events_on_parent: int


async def bundle_examples(
    session: AsyncSession, *, since: datetime, limit: int = 3
) -> list[ParentExample]:
    live = Order.status.not_in(CANCELLED_STATUSES)
    qty = func.sum(OrderItem.quantity)
    parents = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            qty,
            func.sum(OrderItem.quantity * OrderItem.unit_price),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(MasterSku, MasterSku.id == OrderItem.master_sku_id)
        .where(MasterSku.is_bundle.is_(True), Order.ordered_at >= since, live)
        .group_by(MasterSku.id, MasterSku.sku_code, MasterSku.name)
        .order_by(qty.desc())
        .limit(limit)
    )

    # Aliased: the outer query selects FROM bundle_components too, and an
    # unaliased inner reference would auto-correlate to the outer row and
    # count 1 for every component — hiding exactly the sharing being shown.
    other = aliased(BundleComponent)
    shared = (
        select(func.count())
        .select_from(other)
        .where(other.component_master_sku_id == MasterSku.id)
        .correlate(MasterSku)
        .scalar_subquery()
    )

    examples: list[ParentExample] = []
    for pid, code, name, sold, sales in parents.all():
        comps = await session.execute(
            select(
                MasterSku.id,
                MasterSku.sku_code,
                MasterSku.name,
                BundleComponent.quantity_per,
                func.coalesce(InventorySnapshot.on_hand_qty, 0),
                shared,
            )
            .select_from(BundleComponent)
            .join(MasterSku, MasterSku.id == BundleComponent.component_master_sku_id)
            .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
            .where(BundleComponent.bundle_master_sku_id == pid)
            .order_by(MasterSku.sku_code)
        )
        on_parent = await session.scalar(
            select(func.count())
            .select_from(InventoryEvent)
            .where(InventoryEvent.master_sku_id == pid, InventoryEvent.occurred_at >= since)
        )
        examples.append(
            ParentExample(
                master_sku_id=pid,
                sku_code=code,
                name=name,
                quantity=int(sold),
                sales=Decimal(sales),
                components=[Component(*c) for c in comps.all()],
                events_on_parent=int(on_parent or 0),
            )
        )
    return examples


async def trace_latest_line(
    session: AsyncSession, parent_id: int, *, since: datetime
) -> tuple[Any, list[Any]] | None:
    """The newest live order line of the parent, and the events it wrote."""
    line = (
        await session.execute(
            select(
                Order.channel,
                Order.channel_order_id,
                Order.ordered_at,
                OrderItem.line_id,
                OrderItem.quantity,
                OrderItem.unit_price,
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                OrderItem.master_sku_id == parent_id,
                Order.ordered_at >= since,
                Order.status.not_in(CANCELLED_STATUSES),
            )
            .order_by(Order.ordered_at.desc())
            .limit(1)
        )
    ).first()
    if line is None:
        return None
    events = await session.execute(
        select(MasterSku.sku_code, InventoryEvent.event_type, InventoryEvent.quantity_delta)
        .join(MasterSku, MasterSku.id == InventoryEvent.master_sku_id)
        .where(
            InventoryEvent.source_order_id == line.channel_order_id,
            InventoryEvent.source_line_id == line.line_id,
        )
        .order_by(InventoryEvent.id)
    )
    return line, list(events.all())


def print_bundle_examples(
    examples: list[ParentExample], trace: tuple[Any, list[Any]] | None
) -> None:
    print(f"\n  === 3. セット・共有在庫の実例  直近{BUNDLE_DAYS}日に売れた親 ===")
    if not examples:
        print("  該当する親商品の販売がありません。期間を延ばすか、別の日に実行してください。")
        return
    for ex in examples:
        print(f"\n  親  {ex.sku_code}  {ex.name[:40]}")
        print(f"      販売 {ex.quantity}点 / {ex.sales:,.0f}円 — 売上はこの親に計上")
        print(f"      親自身に書かれた在庫イベント: {ex.events_on_parent}件")
        for c in ex.components:
            print(
                f"      構成品 {c.sku_code} x{c.quantity_per}  在庫 {c.on_hand}"
                f"  この構成品を使う親 {c.shared_by}件  {c.name[:30]}"
            )
        print(f"      画面: /admin/analytics/sku/{ex.master_sku_id}")
        for c in ex.components:
            print(f"            /admin/events?master_sku_id={c.master_sku_id}")

    if trace is None:
        return
    line, events = trace
    first = examples[0]
    print(f"\n  --- 最新の注文明細を追跡: {first.sku_code} ---")
    print(
        f"    {line.channel} 注文番号 {line.channel_order_id}  "
        f"{(line.ordered_at + timedelta(hours=9)):%Y-%m-%d %H:%M} JST  "
        f"{line.quantity}点 x {line.unit_price:,.0f}円"
    )
    if not events:
        print("    ★ この明細の在庫イベントがありません")
    for code, event_type, delta in events:
        print(f"    在庫イベント  {code:<20}{event_type:<24}{delta:+}")


# --- run -------------------------------------------------------------------


async def run(*, days: int = DEFAULT_DAYS, out_dir: Path | None = None) -> int:
    now = datetime.now(UTC)
    period = whole_days_before_today(days, now=now)
    async with async_session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        by_channel = await channel_days(session, period)
        basis = await price_basis(session, since=now - timedelta(days=PRICE_DAYS))
        bundle_since = now - timedelta(days=BUNDLE_DAYS)
        examples = await bundle_examples(session, since=bundle_since)
        trace = (
            await trace_latest_line(session, examples[0].master_sku_id, since=bundle_since)
            if examples
            else None
        )

    print_channel_days(period, by_channel)
    if out_dir is not None:
        print(f"\n  CSV -> {write_csv(out_dir, period, by_channel)}")
    print_price_basis(basis)
    print_bundle_examples(examples, trace)

    log.info(
        "review_materials.done",
        channel_days=len(by_channel),
        rakuten_tax=basis.rakuten_verdict,
        shopify_tax=basis.shopify_verdict,
        deleted_lines=basis.deleted_lines,
        bundle_examples=len(examples),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only material for the acceptance review")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="whole JST days back from today")
    p.add_argument("--out", default=None, help="directory to write the per-channel CSV into")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(days=args.days, out_dir=Path(args.out) if args.out else None)))


if __name__ == "__main__":
    main()

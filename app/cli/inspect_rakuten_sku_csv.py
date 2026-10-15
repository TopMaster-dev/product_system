"""Read-only: what the RMS item export tells us about SKU-level stock (P2-038).

The shop exports its whole catalogue from RMS as `dl-normal-item`. That file turns
out to carry everything the SKU-level stock push needs, so the mapping no longer
depends on an API we are not yet permitted to call:

  商品管理番号 (商品URL)   manageNumber — the product key
  SKU管理番号               variantId    — the SKU key the stock API writes against
  システム連携用SKU番号      merchantDefinedSkuId — the shop's own code, which is
                            also the CROSS MALL 商品SKUコード and therefore joins
                            straight to our master SKUs
  在庫数                    Rakuten's CURRENT stock for that SKU

Verified against the live API on 2026-10-15: all five (manageNumber, variantId)
pairs seen in recent orders appear in the file, and their システム連携用SKU番号
equals the `merchantDefinedSkuId` the item API returned for the same SKU.

WHY THE PAIR AND NOT THE SKU ALONE: 132 SKU管理番号 are reused across different
products. Keyed on the SKU alone the push would be ambiguous for those; keyed on
the pair all 1,107 rows are unique.

This reports three things and writes nothing:

1. **Coverage** — how many Rakuten SKUs join to one of our masters, and by which
   route. What is left over is the real mapping backlog.
2. **Stock drift** — Rakuten's figure against ours, per SKU. This is the shadow
   comparison that has to be read BEFORE any stock is pushed: the push sets an
   absolute quantity, so a mapping error shows up here as a wild difference
   rather than on the shop's live pages.
3. **Order coverage** — how many unmapped Rakuten order lines these pairs would
   resolve.

    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_sku_csv ^
        -Args "--csv csv_file/dl-normal-item_20261015101912-1.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, InventorySnapshot, MasterSku, Order, OrderItem
from app.services.rakuten_catalogue import (
    RAKUTEN,
    RakutenSku,
    links_from_orders,
    read_catalogue,
)

log = get_logger(__name__)

#: Rows the drift table prints before summarising.
MAX_ROWS = 25


# --- joining to our own masters ----------------------------------------------------


@dataclass(slots=True)
class Match:
    sku: RakutenSku
    master_id: int | None = None
    master_code: str = ""
    #: How the join was made, for the coverage report.
    via: str = ""
    on_hand: int | None = None

    @property
    def drift(self) -> int | None:
        return None if self.on_hand is None else self.sku.quantity - self.on_hand


@dataclass(slots=True)
class Coverage:
    matches: list[Match] = field(default_factory=list)
    by_route: Counter[str] = field(default_factory=Counter)

    @property
    def matched(self) -> list[Match]:
        return [m for m in self.matches if m.master_id is not None]

    @property
    def unmatched(self) -> list[Match]:
        return [m for m in self.matches if m.master_id is None]


VIA_ORDERS = "受注実績 (実際に売れたSKU)"
VIA_MERCHANT = "システム連携用SKU番号"
VIA_VARIANT = "SKU管理番号"
VIA_PRODUCT = "既存の商品単位マッピング (SKUを特定できません)"


async def join_to_masters(session: AsyncSession, skus: list[RakutenSku]) -> Coverage:
    """Four routes, most trustworthy first. Each row is attributed to exactly the
    route that found it, so the report says how the catalogue would be covered."""
    proven = await links_from_orders(session)
    #: Ordered so a code collision resolves the same way on every run.
    master_rows = await session.execute(
        select(MasterSku.id, MasterSku.sku_code).order_by(MasterSku.id)
    )
    by_code = {code.strip().lower(): (mid, code) for mid, code in master_rows.all()}
    existing = {
        (sku or "").strip(): mid
        for sku, mid in (
            await session.execute(
                select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.master_sku_id).where(
                    ChannelSkuMapping.channel == RAKUTEN,
                    ChannelSkuMapping.is_active.is_(True),
                )
            )
        ).all()
    }
    snapshot_rows = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty)
    )
    on_hand: dict[int, int] = dict(snapshot_rows.all())  # type: ignore[arg-type]

    code_by_id = dict(by_code.values())

    coverage = Coverage()
    for sku in skus:
        match = Match(sku=sku)
        merchant = sku.merchant_sku_id.strip().lower()
        proven_master = proven.get((sku.manage_number, sku.variant_id))
        if proven_master is not None:
            match.master_id = proven_master
            match.master_code = code_by_id.get(proven_master, "")
            match.via = VIA_ORDERS
        elif merchant and merchant in by_code:
            match.master_id, match.master_code = by_code[merchant]
            match.via = VIA_MERCHANT
        elif sku.variant_id.strip().lower() in by_code:
            match.master_id, match.master_code = by_code[sku.variant_id.strip().lower()]
            match.via = VIA_VARIANT
        elif sku.manage_number in existing:
            # The product-level mapping we hold today. It cannot tell this SKU
            # from its siblings, so it is a fallback, not an answer.
            match.master_id = existing[sku.manage_number]
            match.via = VIA_PRODUCT
        if match.master_id is not None:
            match.on_hand = on_hand.get(match.master_id)
        coverage.matches.append(match)
        coverage.by_route[match.via or "未対応"] += 1
    return coverage


async def unmapped_order_lines(session: AsyncSession) -> tuple[int, int]:
    """(unmapped Rakuten order lines, distinct 商品管理番号 among them)."""
    lines = await session.scalar(
        select(func.count())
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == RAKUTEN, OrderItem.master_sku_id.is_(None))
    )
    codes = await session.scalar(
        select(func.count(func.distinct(OrderItem.channel_sku)))
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == RAKUTEN, OrderItem.master_sku_id.is_(None))
    )
    return int(lines or 0), int(codes or 0)


# --- report -------------------------------------------------------------------------


def print_catalogue(skus: list[RakutenSku], problems: list[str]) -> None:
    products = {s.manage_number for s in skus}
    placeholders = [s for s in skus if s.is_placeholder]
    reused = Counter(s.variant_id for s in skus)
    print("\n  === 楽天の商品データ (RMS dl-normal-item) — 読み取りのみ ===")
    print("\n  1. ファイルの内容")
    print(f"     商品 {len(products)}件 / SKU {len(skus)}件")
    print(f"     在庫の合計 {sum(s.quantity for s in skus):,}点")
    print(f"     仮値のSKU管理番号 (r-sku…) {len(placeholders)}件 — 押し出しには支障ありません")
    print(
        f"     複数の商品で使い回されているSKU管理番号 {sum(1 for v in reused.values() if v > 1)}件"
    )
    print("     そのため在庫の指定は「商品管理番号とSKU管理番号の組」で行います")
    for problem in problems[:10]:
        print(f"     ★ {problem}")


def print_coverage(coverage: Coverage) -> None:
    total = len(coverage.matches)
    print(f"\n  2. 当システムの商品との対応 ({len(coverage.matched)}/{total}件)")
    for route, count in coverage.by_route.most_common():
        print(f"     {route:<40}{count:>6}件")
    unmatched = coverage.unmatched
    if unmatched:
        print(f"\n     対応づけできないSKU {len(unmatched)}件 (先頭{min(len(unmatched), 10)}件)")
        for match in unmatched[:10]:
            sku = match.sku
            print(
                f"       {sku.manage_number:<14}{sku.variant_id:<16}"
                f"{sku.merchant_sku_id or '—':<18}{sku.name[:26]}"
            )


def print_ambiguity(coverage: Coverage) -> None:
    """Rakuten SKUs that would be told the SAME master's stock.

    This is the one finding that must block a push. Four SKUs of one product all
    pointing at one master means our figure would be written to all four, and
    the shop would show stock it does not have — on four live pages at once.
    """
    claims: dict[int, list[Match]] = {}
    for match in coverage.matched:
        if match.master_id is not None:
            claims.setdefault(match.master_id, []).append(match)
    shared = {mid: ms for mid, ms in claims.items() if len(ms) > 1}
    print("\n  4. 同じ商品を指している楽天SKU")
    if not shared:
        print("     ありません。対応づけは1対1です")
        return
    skus = sum(len(ms) for ms in shared.values())
    print(f"     ★ {skus}件の楽天SKUが、{len(shared)}件の当システム商品を共有しています")
    print("     このまま在庫を反映すると、同じ数量が複数のSKUに書き込まれます")
    worst = sorted(shared.values(), key=len, reverse=True)[:5]
    for group in worst:
        code = group[0].master_code or f"master#{group[0].master_id}"
        pairs = ", ".join(f"{m.sku.manage_number}/{m.sku.variant_id}" for m in group[:5])
        more = f" ほか{len(group) - 5}件" if len(group) > 5 else ""
        print(f"       {code:<20}{len(group)}件  {pairs}{more}")


def print_drift(coverage: Coverage) -> None:
    comparable = [m for m in coverage.matched if m.on_hand is not None and m.via != ""]
    exact = [m for m in comparable if m.drift == 0]
    differing = sorted(
        (m for m in comparable if m.drift), key=lambda m: abs(m.drift or 0), reverse=True
    )
    print("\n  5. 在庫数の比較 (楽天とシステムの差)")
    if not comparable:
        print("     比較できるSKUがありません")
        return
    print(
        f"     比較できたSKU {len(comparable)}件 / 一致 {len(exact)}件 / 差あり {len(differing)}件"
    )
    if differing:
        print(
            f"\n     {'商品管理番号':<14}{'SKU管理番号':<16}{'楽天':>8}{'当方':>8}{'差':>8}  商品名"
        )
        for match in differing[:MAX_ROWS]:
            print(
                f"     {match.sku.manage_number:<14}{match.sku.variant_id:<16}"
                f"{match.sku.quantity:>8}{match.on_hand:>8}{match.drift:>+8}  {match.sku.name[:22]}"
            )
        if len(differing) > MAX_ROWS:
            print(f"     ... ほか {len(differing) - MAX_ROWS}件")
    print(
        "\n     この差は、在庫を楽天へ反映する前に必ず確認してください。"
        "\n     押し出しは絶対値で上書きするため、対応づけの誤りはここに大きな差として現れます。"
    )


async def run(*, csv_path: Path) -> int:
    skus, problems = read_catalogue(csv_path)
    if not skus:
        for problem in problems:
            print(f"  ★ {problem}")
        return 1

    async with async_session_factory() as session:
        coverage = await join_to_masters(session, skus)
        lines, codes = await unmapped_order_lines(session)

    print_catalogue(skus, problems)
    print_coverage(coverage)
    print_ambiguity(coverage)
    print_drift(coverage)

    print("\n  6. 未マッピングの受注")
    print(f"     楽天の未マッピング明細 {lines}件 / 異なる商品管理番号 {codes}件")
    resolvable = {m.sku.manage_number for m in coverage.matched}
    print(f"     このファイルで商品を特定できる商品管理番号 {len(resolvable)}件")

    log.info(
        "rakuten_sku_csv.done",
        skus=len(skus),
        products=len({s.manage_number for s in skus}),
        matched=len(coverage.matched),
        unmatched=len(coverage.unmatched),
        unmapped_lines=lines,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only analysis of the RMS item export")
    p.add_argument("--csv", required=True, help="dl-normal-item CSV exported from RMS")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(csv_path=Path(args.csv))))


if __name__ == "__main__":
    main()
